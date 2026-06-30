/* Copyright (c) 2026, Oracle and/or its affiliates.

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is designed to work with certain software (including
   but not limited to OpenSSL) that is licensed under separate terms,
   as designated in a particular file or component or in included license
   documentation.  The authors of MySQL hereby grant you an additional
   permission to link the program and your derivative works with the
   separately licensed software that they have either included with
   the program or referenced in the documentation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.

   You should have received a copy of the GNU General Public License
   along with this program; if not, write to the Free Software
   Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA */

#include <gtest/gtest.h>

#include <cstring>

#include "my_alloc.h"
#include "sql/server_component/mysql_command_consumer_imp.h"

namespace mysql_command_consumer_unittest {

/*
  Verify that field_metadata() produces libmysql-compatible MYSQL_FIELD string
  metadata: catalog is "def", and each copied string has its byte length in the
  corresponding length member.
*/
TEST(MysqlCommandConsumerTest, FieldMetadataPopulatesCatalogAndLengths) {
  // Build the minimal DOM result context required by field_metadata().
  MEM_ROOT mem_root;
  MYSQL_DATA result{};
  result.alloc = &mem_root;
  MYSQL_DATA *result_ptr = &result;

  MYSQL_FIELD mysql_field{};
  Dom_ctx ctx{};
  ctx.m_result = &result_ptr;
  ctx.m_fields = &mysql_field;

  Field_metadata field{.db_name = "database",
                       .table_name = "table_alias",
                       .org_table_name = "table_name",
                       .col_name = "column_alias",
                       .org_col_name = "column_name",
                       .length = 42,
                       .charsetnr = 255,
                       .flags = 0,
                       .decimals = 0,
                       .type = MYSQL_TYPE_LONG};

  EXPECT_FALSE(mysql_command_consumer_dom_imp::field_metadata(
      reinterpret_cast<SRV_CTX_H>(&ctx), &field, nullptr));

  EXPECT_STREQ("def", mysql_field.catalog);
  EXPECT_EQ(3U, mysql_field.catalog_length);
  EXPECT_STREQ(field.db_name, mysql_field.db);
  EXPECT_EQ(std::strlen(field.db_name), mysql_field.db_length);
  EXPECT_STREQ(field.table_name, mysql_field.table);
  EXPECT_EQ(std::strlen(field.table_name), mysql_field.table_length);
  EXPECT_STREQ(field.org_table_name, mysql_field.org_table);
  EXPECT_EQ(std::strlen(field.org_table_name), mysql_field.org_table_length);
  EXPECT_STREQ(field.col_name, mysql_field.name);
  EXPECT_EQ(std::strlen(field.col_name), mysql_field.name_length);
  EXPECT_STREQ(field.org_col_name, mysql_field.org_name);
  EXPECT_EQ(std::strlen(field.org_col_name), mysql_field.org_name_length);
}

}  // namespace mysql_command_consumer_unittest
