# Copyright 2015 Tesora Inc.
# All Rights Reserved.
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

from proboscis import test

from trove.tests.api.instances import InstanceSetup
from trove.tests import PRE_INSTANCES
from trove.tests.scenario.groups.test_group import TestGroup


GROUP = "scenario.instance_create_group"


@test(depends_on_classes=[InstanceSetup], runs_after_groups=[PRE_INSTANCES],
      groups=[GROUP])
class InstanceCreateGroup(TestGroup):

    def __init__(self):
        super(InstanceCreateGroup, self).__init__(
            'instance_create_runners', 'InstanceCreateRunner')

    @test
    def create_empty_instance(self):
        """Create an empty instance."""
        self.test_runner.run_empty_instance_create()

    @test(runs_after=[create_empty_instance])
    def create_initial_configuration(self):
        """Create a configuration group for a new initialized instance."""
        self.test_runner.run_initial_configuration_create()

    @test(runs_after=[create_initial_configuration])
    def create_initialized_instance(self):
        """Create an instance with initial properties."""
        self.test_runner.run_initialized_instance_create()

    @test(runs_after=[create_initialized_instance])
    def create_error_instance(self):
        """Create an instance in error state."""
        self.test_runner.run_create_error_instance()

    @test(runs_after=[create_error_instance])
    def wait_for_error_instance(self):
        """Wait for the error instance to fail."""
        self.test_runner.run_wait_for_error_instance()

    @test(depends_on=[wait_for_error_instance])
    def validate_error_instance(self):
        """Validate the error instance fault message."""
        self.test_runner.run_validate_error_instance()

    @test(runs_after=[validate_error_instance])
    def delete_error_instance(self):
        """Delete the error instance."""
        self.test_runner.run_delete_error_instance()

    @test(runs_after=[delete_error_instance])
    def wait_for_instances(self):
        """Waiting for all instances to become active."""
        self.test_runner.wait_for_created_instances()

    @test(depends_on=[wait_for_instances])
    def add_initialized_instance_data(self):
        """Add data to the initialized instance."""
        self.test_runner.run_add_initialized_instance_data()

    @test(runs_after=[add_initialized_instance_data])
    def validate_initialized_instance(self):
        """Validate the initialized instance data and properties."""
        self.test_runner.run_validate_initialized_instance()

    @test(runs_after=[validate_initialized_instance])
    def delete_initialized_instance(self):
        """Delete the initialized instance."""
        self.test_runner.run_initialized_instance_delete()

    @test(runs_after=[delete_initialized_instance])
    def wait_for_error_init_delete(self):
        """Wait for the initialized and error instances to be gone."""
        self.test_runner.run_wait_for_error_init_delete()

    @test(depends_on=[create_initial_configuration],
          runs_after=[wait_for_error_init_delete])
    def delete_initial_configuration(self):
        """Delete the initial configuration group."""
        self.test_runner.run_initial_configuration_delete()
