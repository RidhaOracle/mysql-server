# Copyright (c) 2026, Oracle and/or its affiliates.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2.0,
# as published by the Free Software Foundation.
#
# This program is designed to work with certain software (including
# but not limited to OpenSSL) that is licensed under separate terms,
# as designated in a particular file or component or in included license
# documentation.  The authors of MySQL hereby grant you an additional
# permission to link the program and your derivative works with the
# separately licensed software that they have either included with
# the program or referenced in the documentation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License, version 2.0, for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA

MY_CHECK_CXX_COMPILER_WARNING("-Wno-unused-local-typedefs" HAS_WARN_FLAG)
IF(HAS_WARN_FLAG)
  STRING_APPEND(CMAKE_CXX_FLAGS " ${HAS_WARN_FLAG}")
ENDIF()

INCLUDE_DIRECTORIES(SYSTEM ${GMOCK_INCLUDE_DIRS})

ADD_STATIC_LIBRARY(mysql_scheduler_server_test_support
  ../../../../libs/mysql/scheduler/dependency_tracker_single_predecessor_example.cpp
  LINK_LIBRARIES mysql_concurrency_server extra::unordered_dense
)

# Add tests
SET(TESTS
  sharded_counter
  scheduler
  scheduler_perf
  scheduler_perf_dependencies
  scheduler_sync
  scheduler_task_phases
  scheduler_throughput
  task_registry
  stat_monitor
)

FOREACH(test ${TESTS})
  MYSQL_ADD_EXECUTABLE(${test}-t ${test}-t.cc
    ADD_TEST ${test}
    LINK_LIBRARIES mysql_scheduler gunit_small
    COMPILE_DEFINITIONS "${DISABLE_PSI_DEFINITIONS}" "STANDALONE_LIBS_MYSQL")
ENDFOREACH()

FOREACH(test ${TESTS})
  # When the unit-test library is shared, link to the library file directly
  # to avoid pulling the same scheduler objects into the test executable
  # through the target's transitive link interface, which trips ASan ODR
  # checks. Keep linking to the target otherwise so platform-specific target
  # properties still apply. Add the Abseil sampler explicitly because this
  # path also bypasses the target's transitive shared-library dependencies.
  IF(WITH_SHARED_UNITTEST_LIBRARY)
    SET(SCHEDULER_SERVER_UNITTEST_LINK
      $<TARGET_FILE:server_unittest_library> absl::hashtablez_sampler)
  ELSE()
    SET(SCHEDULER_SERVER_UNITTEST_LINK server_unittest_library)
  ENDIF()

  MYSQL_ADD_EXECUTABLE(${test}-srv-t ${test}-t.cc
    ADD_TEST ${test}
    LINK_LIBRARIES mysql_concurrency_server
      mysql_scheduler_server_test_support gunit_large
      ${SCHEDULER_SERVER_UNITTEST_LINK})
  ADD_DEPENDENCIES(${test}-srv-t server_unittest_library)
ENDFOREACH()
