#  Copyright 2013 Mirantis Inc.
#  All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import stat
import tempfile
from cassandra import OperationTimedOut
from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster
from cassandra.cluster import NoHostAvailable
from oslo_utils import netutils
from trove.common import cfg
from trove.common import utils
from trove.common import exception
from trove.common import instance as rd_instance
from trove.common.i18n import _
from trove.guestagent import pkg
from trove.guestagent.common import operating_system
from trove.guestagent.datastore.experimental.cassandra import system
from trove.guestagent.datastore import service
from trove.guestagent.db import models
from trove.openstack.common import log as logging


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

packager = pkg.Package()


class CassandraApp(object):
    """Prepares DBaaS on a Guest container."""

    _CONF_AUTH_SEC = 'authentication'
    _CONF_USR_KEY = 'username'
    _CONF_PWD_KEY = 'password'
    _CONF_DIR_MODS = stat.S_IRWXU
    _CONF_FILE_MODS = stat.S_IRUSR

    def __init__(self, status):
        """By default login with root no password for initial setup."""
        self.state_change_wait_time = CONF.state_change_wait_time
        self.status = status

    def install_if_needed(self, packages):
        """Prepare the guest machine with a cassandra server installation."""
        LOG.info(_("Preparing Guest as a Cassandra Server"))
        if not packager.pkg_is_installed(packages):
            self._install_db(packages)
        LOG.debug("Cassandra install_if_needed complete")

    def complete_install_or_restart(self):
        self.status.end_install_or_restart()

    def _enable_db_on_boot(self):
        utils.execute_with_timeout(system.ENABLE_CASSANDRA_ON_BOOT,
                                   shell=True)

    def _disable_db_on_boot(self):
        utils.execute_with_timeout(system.DISABLE_CASSANDRA_ON_BOOT,
                                   shell=True)

    def init_storage_structure(self, mount_point):
        try:
            cmd = system.INIT_FS % mount_point
            utils.execute_with_timeout(cmd, shell=True)
        except exception.ProcessExecutionError:
            LOG.exception(_("Error while initiating storage structure."))

    def start_db(self, update_db=False):
        self._enable_db_on_boot()
        try:
            utils.execute_with_timeout(system.START_CASSANDRA,
                                       shell=True)
        except exception.ProcessExecutionError:
            LOG.exception(_("Error starting Cassandra"))
            pass

        if not (self.status.
                wait_for_real_status_to_change_to(
                rd_instance.ServiceStatuses.RUNNING,
                self.state_change_wait_time,
                update_db)):
            try:
                utils.execute_with_timeout(system.CASSANDRA_KILL,
                                           shell=True)
            except exception.ProcessExecutionError:
                LOG.exception(_("Error killing Cassandra start command."))
            self.status.end_install_or_restart()
            raise RuntimeError(_("Could not start Cassandra"))

    def stop_db(self, update_db=False, do_not_start_on_reboot=False):
        if do_not_start_on_reboot:
            self._disable_db_on_boot()
        utils.execute_with_timeout(system.STOP_CASSANDRA,
                                   shell=True,
                                   timeout=system.SERVICE_STOP_TIMEOUT)

        if not (self.status.wait_for_real_status_to_change_to(
                rd_instance.ServiceStatuses.SHUTDOWN,
                self.state_change_wait_time, update_db)):
            LOG.error(_("Could not stop Cassandra."))
            self.status.end_install_or_restart()
            raise RuntimeError(_("Could not stop Cassandra."))

    def restart(self):
        try:
            self.status.begin_restart()
            LOG.info(_("Restarting Cassandra server."))
            self.stop_db()
            self.start_db()
        finally:
            self.status.end_install_or_restart()

    def _install_db(self, packages):
        """Install cassandra server"""
        LOG.debug("Installing cassandra server.")
        packager.pkg_install(packages, None, system.INSTALL_TIMEOUT)
        LOG.debug("Finished installing Cassandra server")

    def configure_superuser_access(self):
        current_superuser = CassandraApp.get_current_superuser()
        cassandra = models.CassandraUser(system.DEFAULT_SUPERUSER_NAME,
                                         utils.generate_random_password())
        self.__create_cqlsh_config({self._CONF_AUTH_SEC:
                                   {self._CONF_USR_KEY: cassandra.name,
                                    self._CONF_PWD_KEY: cassandra.password}})
        CassandraAdmin(current_superuser).alter_user_password(cassandra)
        self.status.set_superuser(cassandra)

        return cassandra

    def __create_cqlsh_config(self, sections):
        config_path = self._get_cqlsh_conf_path()
        config_dir = os.path.dirname(config_path)
        if not os.path.exists(config_dir):
            os.mkdir(config_dir, self._CONF_DIR_MODS)
        else:
            os.chmod(config_dir, self._CONF_DIR_MODS)
        operating_system.write_config_file(config_path, sections)
        os.chmod(config_path, self._CONF_FILE_MODS)

    @classmethod
    def get_current_superuser(self):
        """
        Build the Trove superuser.
        Use the stored credentials.
        If not available fall back to the defaults.
        """
        if CassandraApp.has_user_config():
            return CassandraApp.__load_current_superuser()

        return models.CassandraUser(system.DEFAULT_SUPERUSER_NAME,
                                    system.DEFAULT_SUPERUSER_PASSWORD)

    @classmethod
    def has_user_config(self):
        """
        Return TRUE if there is a client configuration file available
        on the guest.
        """
        return os.path.exists(self._get_cqlsh_conf_path())

    @classmethod
    def __load_current_superuser(self):
        config = operating_system.read_config_file(self._get_cqlsh_conf_path())
        return models.CassandraUser(
            config[self._CONF_AUTH_SEC][self._CONF_USR_KEY],
            config[self._CONF_AUTH_SEC][self._CONF_PWD_KEY]
        )

    def write_config(self, config_contents,
                     execute_function=utils.execute_with_timeout,
                     mkstemp_function=tempfile.mkstemp,
                     unlink_function=os.unlink):

        # first securely create a temp file. mkstemp() will set
        # os.O_EXCL on the open() call, and we get a file with
        # permissions of 600 by default.
        (conf_fd, conf_path) = mkstemp_function()

        LOG.debug('Storing temporary configuration at %s.' % conf_path)

        # write config and close the file, delete it if there is an
        # error. only unlink if there is a problem. In normal course,
        # we move the file.
        try:
            operating_system.write_yaml_file(conf_path, config_contents)
            execute_function("sudo", "mv", conf_path, system.CASSANDRA_CONF)
            #TODO(denis_makogon): figure out the dynamic way to discover
            # configs owner since it can cause errors if there is
            # no cassandra user in operating system
            operating_system.update_owner(system.CASSANDRA_OWNER,
                                          system.CASSANDRA_OWNER,
                                          system.CASSANDRA_CONF)
            execute_function("sudo", "chmod", "a+r", system.CASSANDRA_CONF)
        except Exception:
            LOG.exception(
                _("Exception generating Cassandra configuration %s.") %
                conf_path)
            unlink_function(conf_path)
            raise
        finally:
            os.close(conf_fd)

        LOG.info(_('Wrote new Cassandra configuration.'))

    def update_config_with_single(self, key, value):
        """Updates single key:value in 'cassandra.yaml'."""

        yamled = operating_system.read_yaml_file(system.CASSANDRA_CONF)
        yamled.update({key: value})
        LOG.debug("Updating cassandra.yaml with %(key)s: %(value)s."
                  % {'key': key, 'value': value})
        LOG.debug("Dumping YAML to stream.")
        self.write_config(yamled)

    def update_conf_with_group(self, group):
        """Updates group of key:value in 'cassandra.yaml'."""

        yamled = operating_system.read_yaml_file(system.CASSANDRA_CONF)
        for key, value in group.iteritems():
            if key == 'seed':
                (yamled.get('seed_provider')[0].
                 get('parameters')[0].
                 update({'seeds': value}))
            else:
                yamled.update({key: value})
            LOG.debug("Updating cassandra.yaml with %(key)s: %(value)s."
                      % {'key': key, 'value': value})
        LOG.debug("Dumping YAML to stream")
        self.write_config(yamled)

    def make_host_reachable(self):
        updates = {
            'rpc_address': "0.0.0.0",
            'broadcast_rpc_address': netutils.get_my_ipv4(),
            'listen_address': netutils.get_my_ipv4(),
            'seed': netutils.get_my_ipv4()
        }
        self.update_conf_with_group(updates)

    def start_db_with_conf_changes(self, config_contents):
        LOG.info(_("Starting Cassandra with configuration changes."))
        LOG.debug("Inside the guest - Cassandra is running %s."
                  % self.status.is_running)
        if self.status.is_running:
            LOG.error(_("Cannot execute start_db_with_conf_changes because "
                        "Cassandra state == %s.") % self.status)
            raise RuntimeError("Cassandra not stopped.")
        LOG.debug("Initiating config.")
        self.write_config(config_contents)
        self.start_db(True)

    def reset_configuration(self, configuration):
        config_contents = configuration['config_contents']
        LOG.debug("Resetting configuration")
        self.write_config(config_contents)

    @classmethod
    def _get_cqlsh_conf_path(self):
        return os.path.expanduser(system.CQLSH_CONF_PATH)


class CassandraAppStatus(service.BaseDbStatus):

    def __init__(self, superuser):
        """
        :param superuser:        User account the Status uses for connecting
                                 to the database.
        :type superuser:         CassandraUser
        """
        self.__user = superuser

    def set_superuser(self, user):
        self.__user = user

    def _get_actual_db_status(self):
        try:
            with CassandraLocalhostConnection(self.__user):
                return rd_instance.ServiceStatuses.RUNNING
        except NoHostAvailable:
            return rd_instance.ServiceStatuses.SHUTDOWN
        except Exception:
            LOG.exception(_("Error getting Cassandra status."))

        return rd_instance.ServiceStatuses.SHUTDOWN


class CassandraAdmin(object):
    """Handles administrative tasks on the Cassandra database.

    In Cassandra only SUPERUSERS can create other users and grant permissions
    to database resources. Trove uses the 'cassandra' superuser to perform its
    administrative tasks.

    The users it creates are all 'normal' (NOSUPERUSER) accounts.
    The permissions it can grant are also limited to non-superuser operations.
    This is to prevent anybody from creating a new superuser via the Trove API.
    Similarly, all list operations include only non-superuser accounts.
    """

    # Non-superuser grant modifiers.
    __NO_SUPERUSER_MODIFIERS = ('ALTER', 'CREATE', 'DROP', 'MODIFY', 'SELECT')

    def __init__(self, user):
        self.__admin_user = user

    def create_user(self, context, users):
        """
        Create new non-superuser accounts.
        New users are by default granted full access to all database resources.
        """
        with CassandraLocalhostConnection(self.__admin_user) as client:
            for item in users:
                self._create_user_and_grant(client,
                                            self._deserialize_user(item))

    def _create_user_and_grant(self, client, user):
        """
        Create new non-superuser account and grant it full access to its
        databases.
        """
        self._create_user(client, user)
        for db in user.databases:
            self._grant_full_access_on_keyspace(client, db, user)

    def _create_user(self, client, user):
        # Create only NOSUPERUSER accounts here.
        LOG.debug("Creating a new user '%s'." % user.name)
        client.execute("CREATE USER '{}' WITH PASSWORD %s NOSUPERUSER;",
                       (user.name,), (user.password,))

    def delete_user(self, context, user):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            self._drop_user(client, self._deserialize_user(user))

    def _drop_user(self, client, user):
        LOG.debug("Deleting user '%s'." % user.name)
        client.execute("DROP USER '{}';", (user.name, ))

    def get_user(self, context, username, hostname):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            return self._find_user(client, username).serialize()

    def _find_user(self, client, username):
        """
        Lookup a user with a given username.
        Search only in non-superuser accounts.
        Return a new Cassandra user instance or raise if no match is found.
        """
        found = next((user for user in self._get_non_system_users(client)
                      if user.name == username), None)
        if found:
            return found

        raise exception.UserNotFound()

    def list_users(self, context, limit=None, marker=None,
                   include_marker=False):
        """
        List all non-superuser accounts.
        Return an empty set if None.
        """
        with CassandraLocalhostConnection(self.__admin_user) as client:
            return ([user.serialize() for user in
                     self._get_non_system_users(client)], None)

    def _get_non_system_users(self, client):
        """
        Return a set of unique user instances.
        Return only non-superuser accounts. Omit user names on the ignore list.
        """
        return {self._build_user(client, user.name)
                for user in client.execute("LIST USERS;")
                if not user.super and user.name not in CONF.ignore_users}

    def _build_user(self, client, username):
        user = models.CassandraUser(username)
        for keyspace in self._get_available_keyspaces(client):
            found = self._get_permissions_on_keyspace(client, keyspace, user)
            if found:
                user.databases.append(keyspace.serialize())

        return user

    def _get_permissions_on_keyspace(self, client, keyspace, user):
        return {item.permission for item in
                client.execute("LIST ALL PERMISSIONS ON KEYSPACE \"{}\" "
                               "OF '{}' NORECURSIVE;",
                               (keyspace.name, user.name))}

    def grant_access(self, context, username, hostname, databases):
        """
        Grant full access on keyspaces to a given username.
        """
        user = models.CassandraUser(username)
        with CassandraLocalhostConnection(self.__admin_user) as client:
            for db in databases:
                self._grant_full_access_on_keyspace(
                    client, models.CassandraSchema(db), user)

    def revoke_access(self, context, username, hostname, database):
        """
        Revoke all permissions on any database resources from a given username.
        """
        user = models.CassandraUser(username)
        with CassandraLocalhostConnection(self.__admin_user) as client:
            self._revoke_all_access_on_keyspace(
                client, models.CassandraSchema(database), user)

    def _grant_full_access_on_keyspace(self, client, keyspace, user):
        """
        Grant all non-superuser permissions on a keyspace to a given user.
        """
        for access in self.__NO_SUPERUSER_MODIFIERS:
            self._grant_permission_on_keyspace(client, access, keyspace, user)

    def _grant_permission_on_keyspace(self, client, modifier, keyspace, user):
        """
        Grant a non-superuser permission on a keyspace to a given user.
        Raise an exception if the caller attempts to grant a superuser access.
        """
        LOG.debug("Granting '%s' access on '%s' to user '%s'."
                  % (modifier, keyspace.name, user.name))
        if modifier in self.__NO_SUPERUSER_MODIFIERS:
            client.execute("GRANT {} ON KEYSPACE \"{}\" TO '{}';",
                           (modifier, keyspace.name, user.name))
        else:
            raise exception.UnprocessableEntity(
                "Invalid permission modifier (%s). Allowed values are: '%s'"
                % (modifier, ', '.join(self.__NO_SUPERUSER_MODIFIERS)))

    def _revoke_all_access_on_keyspace(self, client, keyspace, user):
        LOG.debug("Revoking all permissions on '%s' from user '%s'."
                  % (keyspace.name, user.name))
        client.execute("REVOKE ALL PERMISSIONS ON KEYSPACE \"{}\" FROM '{}';",
                       (keyspace.name, user.name))

    def update_attributes(self, context, username, hostname, user_attrs):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            user = self._build_user(client, username)
            new_name = user_attrs.get('name')
            new_password = user_attrs.get('password')
            self._update_user(client, user, new_name, new_password)

    def _update_user(self, client, user, new_username, new_password):
        """
        Update a user of a given username.
        Updatable attributes include username and password.
        If a new username is given a new user with that name
        is created and all permissions from the original
        user get transfered to it. The original user is then dropped
        therefore revoking its permissions.
        If only new password is specified the existing user gets altered
        with that password.
        """
        if new_username is not None and user.name != new_username:
            self._rename_user(client, user, new_username, new_password)
        elif new_password is not None and user.password != new_password:
            user.password = new_password
            self._alter_user_password(client, user)

    def _rename_user(self, client, user, new_username, new_password=None):
        """
        Rename a given user also updating its password if given.
        Transfer the current permissions to the new username.
        Drop the old username therefore revoking its permissions.
        """
        LOG.debug("Renaming user '%s' to '%s'" % (user.name, new_username))
        new_user = models.CassandraUser(new_username,
                                        new_password or user.password)
        new_user.databases.extend(user.databases)
        self._create_user_and_grant(client, new_user)
        self._drop_user(client, user)

    def alter_user_password(self, user):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            self._alter_user_password(client, user)

    def change_passwords(self, context, users):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            for user in users:
                self._alter_user_password(client, self._deserialize_user(user))

    def _alter_user_password(self, client, user):
        LOG.debug("Changing password of user '%s'." % user.name)
        client.execute("ALTER USER '{}' "
                       "WITH PASSWORD %s;", (user.name,), (user.password,))

    def create_database(self, context, databases):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            for item in databases:
                self._create_single_node_keyspace(
                    client, self._deserialize_keyspace(item))

    def _create_single_node_keyspace(self, client, keyspace):
        """
        Create a single-replica keyspace.

        Cassandra stores replicas on multiple nodes to ensure reliability and
        fault tolerance. All replicas are equally important;
        there is no primary or master.
        A replication strategy determines the nodes where
        replicas are placed. SimpleStrategy is for a single data center only.
        The total number of replicas across the cluster is referred to as the
        replication factor.

        Replication Strategy:
        'SimpleStrategy' is not optimized for multiple data centers.
        'replication_factor' The number of replicas of data on multiple nodes.
                             Required for SimpleStrategy; otherwise, not used.

        Keyspace names are case-insensitive by default.
        To make a name case-sensitive, enclose it in double quotation marks.
        """
        client.execute("CREATE KEYSPACE \"{}\" WITH REPLICATION = "
                       "{{ 'class' : 'SimpleStrategy', "
                       "'replication_factor' : 1 }};", (keyspace.name,))

    def delete_database(self, context, database):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            self._drop_keyspace(client, self._deserialize_keyspace(database))

    def _drop_keyspace(self, client, keyspace):
        LOG.debug("Dropping keyspace '%s'." % keyspace.name)
        client.execute("DROP KEYSPACE \"{}\";", (keyspace.name,))

    def list_databases(self, context, limit=None, marker=None,
                       include_marker=False):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            return ([keyspace.serialize() for keyspace
                     in self._get_available_keyspaces(client)], None)

    def _get_available_keyspaces(self, client):
        """
        Return a set of unique keyspace instances.
        Omit keyspace names on the ignore list.
        """
        return {models.CassandraSchema(db.keyspace_name)
                for db in client.execute("SELECT * FROM "
                                         "system.schema_keyspaces;")
                if db.keyspace_name not in CONF.ignore_dbs}

    def list_access(self, context, username, hostname):
        with CassandraLocalhostConnection(self.__admin_user) as client:
            return self._find_user(client, username).databases

    def _deserialize_keyspace(self, keyspace_dict):
        if keyspace_dict:
            keyspace = models.CassandraSchema(None)
            keyspace.deserialize(keyspace_dict)
            return keyspace

        return None

    def _deserialize_user(self, user_dict):
        if user_dict:
            user = models.CassandraUser(None)
            user.deserialize(user_dict)
            return user

        return None


class CassandraConnection(object):
    """A wrapper to manage a Cassandra connection."""

    def __init__(self, contact_points, user):
        self.__user = user
        # A Cluster is initialized with a set of initial contact points.
        # After the driver connects to one of the nodes it will automatically
        # discover the rest.
        # Will connect to '127.0.0.1' if None contact points are given.
        self._cluster = Cluster(
            contact_points=contact_points,
            auth_provider=PlainTextAuthProvider(user.name, user.password))
        self.__session = None

    def __enter__(self):
        self.__connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.__disconnect()

    def execute(self, query, identifiers=None, data_values=None, timeout=None):
        """
        Execute a query with a given sequence or dict of data values to bind.
        If a sequence is used, '%s' should be used the placeholder for each
        argument. If a dict is used, '%(name)s' style placeholders must
        be used.
        Only data values should be supplied this way. Other items,
        such as keyspaces, table names, and column names should be set
        ahead of time. Use the '{}' style placeholders and
        'identifiers' parameter for those.
        Raise an exception if the operation exceeds the given timeout (sec).
        There is no timeout if set to None.
        Return a set of rows or an empty list if None.
        """
        if self.__is_active():
            try:
                rows = self.__session.execute(self.__bind(query, identifiers),
                                              data_values, timeout)
                return rows or []
            except OperationTimedOut:
                LOG.error(_("Query execution timed out."))
                raise

        LOG.debug("Cannot perform this operation on a closed connection.")
        raise exception.UnprocessableEntity()

    def __bind(self, query, identifiers):
        if identifiers:
            return query.format(*identifiers)
        return query

    def __connect(self):
        if not self._cluster.is_shutdown:
            LOG.debug("Connecting to a Cassandra cluster as '%s'."
                      % self.__user.name)
            if not self.__is_active():
                self.__session = self._cluster.connect()
            else:
                LOG.debug("Connection already open.")
            LOG.debug("Connected to cluster: '%s'"
                      % self._cluster.metadata.cluster_name)
            for host in self._cluster.metadata.all_hosts():
                LOG.debug("Connected to node: '%s' in rack '%s' at datacenter "
                          "'%s'" % (host.address, host.rack, host.datacenter))
        else:
            LOG.debug("Cannot perform this operation on a terminated cluster.")
            raise exception.UnprocessableEntity()

    def __disconnect(self):
        if not self.__is_active():
            try:
                LOG.debug("Disconnecting from cluster: '%s'"
                          % self._cluster.metadata.cluster_name)
                self._cluster.shutdown()
                self.__session.shutdown()
            except Exception:
                LOG.debug("Failed to disconnect from a Cassandra cluster.")

    def __is_active(self):
        return self.__session and not self.__session.is_shutdown


class CassandraLocalhostConnection(CassandraConnection):
    """
    A connection to the localhost Cassandra server.
    """

    def __init__(self, user):
        super(CassandraLocalhostConnection, self).__init__(None, user)