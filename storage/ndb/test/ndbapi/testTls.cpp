/*
   Copyright (c) 2026, Oracle and/or its affiliates.


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
   Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA
 */

#include <NdbEnv.h>
#include <NDBT.hpp>
#include <NDBT_Find.hpp>
#include <NDBT_Test.hpp>
#include <NDBT_Workingdir.hpp>
#include <NdbMgmd.hpp>
#include <NdbProcess.hpp>
#include <portlib/NdbDir.hpp>
#include "ConfigFactory.hpp"
#include "portlib/ssl_applink.h"
#include "util/TlsKeyManager.hpp"
#include "util/ndb_openssl3_compat.h"
#include "util/require.h"

#define CHECK(x)                                                     \
  if (!(x)) {                                                        \
    fprintf(stderr, "CHECK(" #x ") failed at line: %d\n", __LINE__); \
    return NDBT_FAILED;                                              \
  }

#if OPENSSL_VERSION_NUMBER >= NDB_TLS_MINIMUM_OPENSSL

static const char *exe_valgrind = nullptr;
static const char *arg_valgrind = nullptr;

// Util function that concatenate strings to form a path

static BaseString path(const char *first, ...) {
  BaseString path;
  path.assign(first);

  const char *str;
  va_list args;
  va_start(args, first);
  while ((str = va_arg(args, const char *)) != NULL) {
    path.appfmt("%s%s", DIR_SEPARATOR, str);
  }
  va_end(args);
  return path;
}

class Mgmd {
 protected:
  std::unique_ptr<NdbProcess> m_proc;
  int m_nodeid;
  BaseString m_name;
  BaseString m_exe;
  NdbMgmd m_mgmd_client;
  bool m_verbose{true};

  inline static int no_node_config = 0;

  Mgmd(const Mgmd &other) = delete;

 public:
  Mgmd(int nodeid) : m_nodeid(nodeid) {
    m_name.assfmt("ndb_mgmd_%d", nodeid);

    NDBT_find_ndb_mgmd(m_exe);
  }

  Mgmd() {
    no_node_config = no_node_config + 1;
    m_name.assfmt("ndb_mgmd_autonode_%d", no_node_config);
    NDBT_find_ndb_mgmd(m_exe);
  }

  ~Mgmd() {
    if (m_proc) {
      stop();
    }
  }

  const char *name(void) const { return m_name.c_str(); }

  const char *exe(void) const { return m_exe.c_str(); }

  void verbose(bool f) { m_verbose = f; }

  bool start(const char *working_dir, NdbProcess::Args &args) {
    g_info << "Starting " << name() << " ";
    for (unsigned i = 0; i < args.args().size(); i++)
      g_info << args.args()[i].c_str() << " ";
    g_info << endl;

    if (exe_valgrind == 0) {
      m_proc = NdbProcess::create(name(), exe(), working_dir, args);
    } else {
      NdbProcess::Args copy;
      if (arg_valgrind) {
        copy.add(arg_valgrind);
      }
      copy.add(exe());
      copy.add(args);
      m_proc = NdbProcess::create(name(), BaseString(exe_valgrind), working_dir,
                                  copy);
    }
    return (bool)m_proc;
  }

  void common_args(NdbProcess::Args &args, const char *working_dir) {
    args.add("--no-defaults");
    args.add("--configdir=", working_dir);
    args.add("--config-file=", "config.ini");
    args.add("--ndb-nodeid=", m_nodeid);
    args.add("--nodaemon");
    args.add("--log-name=", name());
    if (m_verbose) args.add("--verbose");
  }

  bool start_from_config_ini(const char *working_dir,
                             const char *first_extra_arg = NULL, ...) {
    NdbProcess::Args args;
    common_args(args, working_dir);

    if (first_extra_arg) {
      // Append any extra args
      va_list extra_args;
      const char *str = first_extra_arg;
      va_start(extra_args, first_extra_arg);
      do {
        args.add(str);
      } while ((str = va_arg(extra_args, const char *)) != NULL);
      va_end(extra_args);
    }

    return start(working_dir, args);
  }

  bool start(const char *working_dir, const char *first_extra_arg = NULL, ...) {
    NdbProcess::Args args;
    args.add("--no-defaults");
    args.add("--configdir=", working_dir);
    args.add("--ndb-nodeid=", m_nodeid);
    args.add("--nodaemon");
    args.add("--log-name=", name());
    if (m_verbose) args.add("--verbose");

    if (first_extra_arg) {
      // Append any extra args
      va_list extra_args;
      const char *str = first_extra_arg;
      va_start(extra_args, first_extra_arg);
      do {
        args.add(str);
      } while ((str = va_arg(extra_args, const char *)) != NULL);
      va_end(extra_args);
    }

    return start(working_dir, args);
  }

  bool stop(void) {
    g_info << "Stopping " << name() << endl;

    // Diconnect and close our "builtin" client
    m_mgmd_client.close();

    if (!m_proc || !m_proc->stop()) {
      fprintf(stderr, "Failed to stop process %s\n", name());
      return false;  // Can't kill with -9 -> fatal error
    }
    int ret;
    if (!m_proc->wait(ret, 30000)) {
      fprintf(stderr, "Failed to wait for process %s\n", name());
      return false;  // Can't wait after kill with -9 -> fatal error
    }

    if (ret != 9) {
      // The normal case after killing the process with -9 is that wait
      // returns 9, but other return codes may also be returned for example
      // when the process has already terminated itself.
      // The important thing is that the process has terminated, just log return
      // code and continue releasing resources.
      fprintf(stderr, "Process %s stopped with ret: %u\n", name(), ret);
    }

    m_proc.reset();
    return true;
  }

  bool wait(int &ret, int timeout = 30000) {
    g_info << "Waiting for " << name() << endl;

    if (!m_proc || !m_proc->wait(ret, timeout)) {
      fprintf(stderr, "Failed to wait for process %s\n", name());
      return false;
    }

    m_proc.reset();
    return true;
  }

  const BaseString connectstring(const Properties &config) {
    const char *hostname;
    require(get_section_string(config, m_name.c_str(), "HostName", &hostname));

    Uint32 port;
    require(get_section_uint32(config, m_name.c_str(), "PortNumber", &port));

    BaseString constr;
    constr.assfmt("%s:%d", hostname, port);
    return constr;
  }

  bool connect(const Properties &config, int num_retries = 60,
               int retry_delay_in_seconds = 1) {
    BaseString constr = connectstring(config);
    g_info << "Connecting to " << name() << " @ " << constr.c_str() << endl;

    return m_mgmd_client.connect(constr.c_str(), num_retries,
                                 retry_delay_in_seconds);
  }

  int client_start_tls(struct ssl_ctx_st *ctx) {
    return m_mgmd_client.start_tls(ctx);
  }

  bool wait_confirmed_config(int timeout = 30) {
    if (!m_mgmd_client.is_connected()) {
      g_err << "wait_confirmed_config: not connected!" << endl;
      return false;
    }

    int retries = 0;
    Config conf;
    while (!m_mgmd_client.get_config(conf)) {
      retries++;

      if (retries == timeout * 10) {
        g_err << "wait_confirmed_config: Failed to get config within "
              << timeout << " seconds" << endl;
        return false;
      }

      g_err << "Failed to get config, sleeping" << endl;
      NdbSleep_MilliSleep(100);
    }
    g_info << "wait_confirmed_config: ok" << endl;
    return true;
  }

  NdbMgmHandle handle() { return m_mgmd_client.handle(); }

  NdbSocket convert_to_transporter() {
    return m_mgmd_client.convert_to_transporter();
  }

 private:
  bool get_section_string(const Properties &config, const char *section_name,
                          const char *key, const char **value) const {
    const Properties *section;
    if (!config.get(section_name, &section)) return false;

    if (!section->get(key, value)) return false;
    return true;
  }

  bool get_section_uint32(const Properties &config, const char *section_name,
                          const char *key, Uint32 *value) const {
    const Properties *section;
    if (!config.get(section_name, &section)) return false;

    if (!section->get(key, value)) return false;
    return true;
  }
};

class Ndbd : public Mgmd {
 public:
  Ndbd(int nodeid) : Mgmd(nodeid), m_args() {
    m_args.add("--ndb-nodeid=", m_nodeid);
    m_args.add("--foreground");
    m_args.add("--loose-core-file=0");
    m_name.assfmt("ndbd_%d", nodeid);
    NDBT_find_ndbd(m_exe);
  }

  NdbProcess::Args &args() { return m_args; }

  void set_connect_string(const BaseString &connect_string) {
    m_args.add("-c");
    m_args.add(connect_string.c_str());
  }

  bool start(const char *working_dir, const BaseString &connect_string) {
    set_connect_string(connect_string);
    return Mgmd::start(working_dir, m_args);
  }

  bool wait_started(NdbMgmHandle &mgm_handle, int timeout = 30,
                    int node_index = 0) {
    ndb_mgm_node_type node_types[2] = {NDB_MGM_NODE_TYPE_NDB,
                                       NDB_MGM_NODE_TYPE_UNKNOWN};

    int retries = 0;
    while (retries++ < timeout) {
      ndb_mgm_cluster_state *cs = ndb_mgm_get_status2(mgm_handle, node_types);
      if (cs) {
        ndb_mgm_node_state *ndbd_status = cs->node_states + node_index;
        if (ndbd_status->node_status == NDB_MGM_NODE_STATUS_STARTED) {
          g_info << "Node: %d, get status Ok (NODE_STATUS_STARTED)" << m_nodeid
                 << endl;
          free(cs);
          return true;
        }
        free(cs);
      }
      NdbSleep_MilliSleep(1000);
    }
    g_info << "Node: %d, timeout waiting to reach status NODE_STATUS_STARTED"
           << m_nodeid << endl;
    return false;
  }

 private:
  NdbProcess::Args m_args;
};

static bool create_CA(NDBT_Workingdir &wd, const BaseString &exe) {
  int ret;
  NdbProcess::Args args;

  args.add("--passphrase=", "Trondheim");
  args.add("--create-CA");
  args.add("--CA-search-path=", wd.path());
  auto proc = NdbProcess::create("Create CA", exe, wd.path(), args);
  bool r = proc->wait(ret, 10000);

  return (r && (ret == 0));
}

static bool sign_tls_keys(NDBT_Workingdir &wd) {
  int ret;
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);

  /* Find executable */
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create CA */
  if (!create_CA(wd, exe)) return false;

  /* Create keys and certificates for all nodes in config */
  NdbProcess::Args args;
  args.add("--config-file=", cfg_path.c_str());
  args.add("--passphrase=", "Trondheim");
  args.add("--ndb-tls-search-path=", wd.path());
  args.add("--create-key");
  auto proc = NdbProcess::create("Create Keys", exe, wd.path(), args);
  bool r = proc->wait(ret, 10000);
  return (r && (ret == 0));
}

/* Create a certificate for node_type that will expire after cert_duration
 */
static bool create_expiring_cert(NDBT_Workingdir &wd, const BaseString &exe,
                                 const BaseString node_type,
                                 const BaseString cert_duration) {
  int ret;

  NdbProcess::Args args;
  args.add("--create-key");
  args.add("--ndb-tls-search-path=", wd.path());
  args.add("--passphrase=", "Trondheim");
  args.add("-l");  // no-config mode
  args.add("--node-type=", node_type.c_str());
  args.add("--bind-host=", 0);
  args.add("--duration=", cert_duration.c_str());

  auto proc = NdbProcess::create("Create Keys", exe, wd.path(), args);
  bool r = proc->wait(ret, 10000);
  return (r && (ret == 0));
}

/* Create an expired certificate for a data node
 */
inline bool create_expired_cert(NDBT_Workingdir &wd) {
  /* Find executable */
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create CA */
  if (!create_CA(wd, exe)) return false;

  /* use a negative duration to create a cert that has already expired */
  return create_expiring_cert(wd, exe, "db", "-50000");
}

/* Print some information about a cert, and check that its validity is at
   least 120 days. Return true if ok.
*/
static bool check_cert(const NDBT_Workingdir &wd, Node::Type type) {
  static constexpr int MinDuration = 120 * CertLifetime::SecondsPerDay;

  int duration = 0;
  PkiFile::PathName certFile;
  TlsSearchPath searchPath(wd.path());
  if (ActiveCertificate::find(&searchPath, 0, type, certFile)) {
    fprintf(stderr, "Reading cert file: %s \n", certFile.c_str());
    X509 *cert = Certificate::open_one(certFile);
    if (cert) {
      char name[65];
      Certificate::get_common_name(cert, name, sizeof(name));
      const NodeCertificate *nc = NodeCertificate::for_peer(cert);
      if (nc) {
        duration = nc->duration();
        printf(" ... Cert CN:       %s\n", name);
        printf(" ... Cert Duration: %d\n", duration);
        printf(" ... Cert Serial:   %s\n", nc->serial_number().c_str());
        delete nc;
      }
      Certificate::free(cert);
    }
  }
  return (duration >= MinDuration);
}

int runTestSshKeySigning(NDBT_Context *ctx, NDBT_Step *step) {
  /* Skip this test where "ssh localhost" can not be run without user
   * interaction. */
  {
    NdbProcess::Args args;
    auto exe = "ssh";
    args.add("-q");
    args.add("-oBatchMode=yes");
    args.add("localhost");
    args.add("exit");
    auto proc = NdbProcess::create(
        "Probe if `ssh localhost` need user interaction", exe, nullptr, args);
    int ret;
    bool r = proc->wait(ret, 1000);
    if (!r) proc->stop();
    if (r && ret == 255) {
      printf(
          "Skipping test SshKeySigning since `ssh localhost` may need user "
          "interaction.\n");
      return NDBT_SKIPPED;
    }
  }

  NDBT_Workingdir wd("test_mgmd");  // temporary working directory
  Properties config = ConfigFactory::create();
  ConfigFactory::put(config, "ndb_mgmd", 1, "RequireCertificate", "true");
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  /* Find executable */
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create CA */
  if (!create_CA(wd, exe)) return false;

  /* Create keys and certificates for all nodes, via ssh to localhost */
  /* There will be a parent ndb_sign_keys process plus 3 ssh invocations */
  NdbProcess::Args args;
  int ret;
  {
    args.add("--config-file=", cfg_path.c_str());
    args.add("--passphrase=", "Trondheim");
    args.add("--ndb-tls-search-path=", wd.path());
    args.add("--create-key");
    args.add("--remote-exec-path=", exe.c_str());
    args.add("--remote-CA-host=", "localhost");
    auto proc = NdbProcess::create("Create Keys", exe, wd.path(), args);
    bool r = proc->wait(ret, 5000);
    if (!r) proc->stop();
    CHECK(r);
    CHECK(ret == 0);
  }
  CHECK(check_cert(wd, Node::Type::DB));

  /* Sign again, this time using openssl. ndb_sign_keys is called with
     the --remote-openssl option, and with --CA-cert and --CA-key holding
     the full paths to the CA PEM files on the remote server.
  */
  {
    BaseString ca_cert(wd.path());
    ca_cert.append(DIR_SEPARATOR).append(ClusterCertAuthority::CertFile);
    BaseString ca_key(wd.path());
    ca_key.append(DIR_SEPARATOR).append(ClusterCertAuthority::KeyFile);

    args.clear();
    args.add("--config-file=", cfg_path.c_str());
    args.add("--passphrase=", "Trondheim");
    args.add("--ndb-tls-search-path=", wd.path());
    args.add("--remote-openssl");
    args.add("--remote-CA-host=", "localhost");
    args.add("--CA-cert=", ca_cert.c_str());
    args.add("--CA-key=", ca_key.c_str());
    auto proc = NdbProcess::create("OpenSSL", exe, wd.path(), args);
    bool r = proc->wait(ret, 5000);
    if (!r) proc->stop();
    CHECK(r);
    CHECK(ret == 0);
  }
  CHECK(check_cert(wd, Node::Type::DB));

  /* Prove that the certificates created above are usable, by starting the mgmd.
   */
  args.clear();
  Mgmd mgmd(1);
  mgmd.common_args(args, wd.path());
  args.add("--ndb-tls-search-path=", wd.path());
  CHECK(mgmd.start(wd.path(), args));
  CHECK(mgmd.connect(config, 1, 5));
  CHECK(mgmd.wait_confirmed_config());
  CHECK(mgmd.stop());

  return NDBT_OK;
}

int runTestKeySigningTool(NDBT_Context *, NDBT_Step *) {
  NDBT_Workingdir wd("test_mgmd");  // temporary working directory
  Properties config = ConfigFactory::create();
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  /* Find executable */
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create CA */
  if (!create_CA(wd, exe)) return false;

  /* Create key and certificate for node 2 */
  NdbProcess::Args args;
  int ret = -1;
  args.add("--config-file=", cfg_path.c_str());
  args.add("--passphrase=", "Trondheim");
  args.add("--ndb-tls-search-path=", wd.path());
  args.add("--create-key");
  args.add("-n", 2);
  args.add("--CA-tool=", exe.c_str());
  auto proc = NdbProcess::create("Create Keys", exe, wd.path(), args);
  bool r = proc->wait(ret, 10000);
  if (!r) proc->stop();
  CHECK(r);
  CHECK(ret == 0);
  return NDBT_OK;
}

int runTestApiWithoutCert(NDBT_Context *ctx, NDBT_Step *step) {
  NDBT_Workingdir wd("test_tls");  // temporary working directory

  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  CHECK(ConfigFactory::put(config, "ndbd", 2, "RequireTls", "true"));
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  CHECK(sign_tls_keys(wd));

  Mgmd mgmd(1);
  Ndbd ndbd(2);

  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());

  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  CHECK(mgmd.connect(config));             // Connect to management node
  CHECK(mgmd.wait_confirmed_config());     // Wait for configuration

  ndbd.args().add("--ndb-tls-search-path=", wd.path());
  ndbd.start(wd.path(), mgmd.connectstring(config));  // Start data node
  NdbMgmHandle handle = mgmd.handle();
  CHECK(ndbd.wait_started(handle));

  /* API has no TLS context and should fail to connect */
  Ndb_cluster_connection con(mgmd.connectstring(config).c_str());
  con.set_name("api_without_cert");
  int r = con.connect(0, 0, 1);
  CHECK(r == -1);
  printf("ERROR %d: %s\n", con.get_latest_error(), con.get_latest_error_msg());

  ndbd.stop();
  mgmd.stop();
  return NDBT_OK;
}

int runTestNdbdWithoutCert(NDBT_Context *ctx, NDBT_Step *step) {
  NDBT_Workingdir wd("test_mgmd");  // temporary working directory
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);

  Properties config = ConfigFactory::create();
  Properties db;
  db.put("RequireCertificate", "true");
  config.put("DB Default", &db);

  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  Mgmd mgmd(1);
  Ndbd ndbd(2);

  CHECK(mgmd.start_from_config_ini(wd.path()));  // Start management node
  CHECK(mgmd.connect(config));                   // Connect to management node
  CHECK(mgmd.wait_confirmed_config());           // Wait for configuration

  int exit_code;  // Start ndbd; it will fail
  CHECK(ndbd.start(wd.path(), mgmd.connectstring(config)));
  CHECK(ndbd.wait(exit_code, 5000));  // should fail quickly
  require(exit_code == 255);

  CHECK(mgmd.stop());
  return NDBT_OK;
}

int runTestNdbdWithExpiredCert(NDBT_Context *ctx, NDBT_Step *step) {
  NDBT_Workingdir wd("test_tls");  // temporary working directory

  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);

  Properties config = ConfigFactory::create();
  Properties db;
  db.put("RequireCertificate", "true");
  config.put("DB Default", &db);
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  CHECK(create_expired_cert(wd));

  Mgmd mgmd(1);
  Ndbd ndbd(2);

  CHECK(mgmd.start_from_config_ini(wd.path()));  // Start management node
  CHECK(mgmd.connect(config));                   // Connect to management node
  CHECK(mgmd.wait_confirmed_config());           // Wait for configuration

  ndbd.args().add("--ndb-tls-search-path=", wd.path());
  ndbd.start(wd.path(), mgmd.connectstring(config));  // Start data node

  int exit_code;
  CHECK(ndbd.wait(exit_code, 5000));  // should fail quickly
  CHECK(exit_code == 255);

  CHECK(mgmd.stop());
  return NDBT_OK;
}

int runTestNdbdWithCert(NDBT_Context *ctx, NDBT_Step *step) {
  NDBT_Workingdir wd("test_tls");  // temporary working directory

  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  Properties db;
  db.put("RequireCertificate", "true");
  config.put("DB Default", &db);
  ConfigFactory::put(config, "ndb_mgmd", 1, "RequireTls", "true");
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  CHECK(sign_tls_keys(wd));

  Mgmd mgmd(1);
  Ndbd ndbd(2);

  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());
  mgmdArgs.add("--ndb-tls-search-path=", wd.path());

  TlsKeyManager tls_km;
  tls_km.init_mgm_client(wd.path(), Node::Type::DB);

  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  CHECK(mgmd.connect(config));             // Connect to management node
  CHECK(mgmd.client_start_tls(tls_km.ctx()) == 0);  // Start TLS
  CHECK(mgmd.wait_confirmed_config());              // Wait for configuration

  ndbd.args().add("--ndb-tls-search-path=", wd.path());
  ndbd.args().add("--ndb-mgm-tls=strict");
  ndbd.start(wd.path(), mgmd.connectstring(config));  // Start data node
  NdbMgmHandle handle = mgmd.handle();
  CHECK(ndbd.wait_started(handle));

  CHECK(mgmd.stop());
  CHECK(ndbd.stop());
  return NDBT_OK;
}

int runTestStartTls(NDBT_Context *ctx, NDBT_Step *step) {
  NDBT_Workingdir wd("test_tls");  // temporary working directory
  TlsKeyManager tls_km;
  int major, minor, build, r;
  char ver[128];
  static constexpr int len = sizeof(ver);

  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  sign_tls_keys(wd);

  Mgmd mgmd(1);

  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());
  mgmdArgs.add("--ndb-tls-search-path=", wd.path());

  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  CHECK(mgmd.connect(config));             // Connect to management node
  CHECK(mgmd.wait_confirmed_config());     // Wait for configuration

  tls_km.init_mgm_client(wd.path());
  CHECK(tls_km.ctx());

  r = ndb_mgm_get_version(mgmd.handle(), &major, &minor, &build, len, ver);
  CHECK(r == 1);
  printf("Version: %d.%d.%d %s\n", major, minor, build, ver);

  r = ndb_mgm_start_tls(mgmd.handle());
  CHECK(r == -1);  // -1 is "SSL CTX required"
  CHECK(ndb_mgm_get_latest_error(mgmd.handle()) == NDB_MGM_TLS_ERROR);

  r = ndb_mgm_set_ssl_ctx(mgmd.handle(), tls_km.ctx());
  CHECK(r == 0);  // first time setting ctx succeeds
  r = ndb_mgm_set_ssl_ctx(mgmd.handle(), nullptr);
  CHECK(r == -1);  // second time setting ctx fails

  r = ndb_mgm_start_tls(mgmd.handle());
  printf("ndb_mgm_start_tls(): %d\n", r);
  CHECK(r == 0);

  r = ndb_mgm_start_tls(mgmd.handle());
  CHECK(r == -2);  // -2 is "Socket already has TLS"

  /* We have switched to TLS. Now run a command. */
  r = ndb_mgm_get_version(mgmd.handle(), &major, &minor, &build, len, ver);
  CHECK(r == 1);

  /* And run another command. */
  struct ndb_mgm_cluster_state *state = ndb_mgm_get_status(mgmd.handle());
  CHECK(state != nullptr);

  /* Now convert the socket to a transporter */
  NdbSocket s = mgmd.convert_to_transporter();
  CHECK(s.is_valid());
  CHECK(s.close() == 0);

  return NDBT_OK;
}

/* Test the TLS INFO statistics after the TLS auth has failed due to an
   expired server certificate
*/
int runTestTlsStats1(NDBT_Context *ctx, NDBT_Step *step) {
  ndb_mgm_tls_stats stats[3];
  auto print_stats = [](const ndb_mgm_tls_stats &stats) {
    printf("TLS Stats -- accepted:%d upgraded:%d current:%d tls:%d\n",
           stats.accepted, stats.upgraded, stats.current, stats.tls);
  };
  NDBT_Workingdir wd("test_tls");  // temporary working directory
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create a configuration */
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  /* Create certificates that will expire soon */
  CHECK(create_CA(wd, exe));
  CHECK(create_expiring_cert(wd, exe, "mgmd", "8"));  // expires in 8 seconds
  CHECK(create_expiring_cert(wd, exe, "api", "120"));

  /* MGM server */
  Mgmd mgmd(1);
  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());
  mgmdArgs.add("--ndb-tls-search-path=", wd.path());
  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  CHECK(mgmd.connect(config));             // Connect to management node
  CHECK(mgmd.wait_confirmed_config());     // Wait for configuration

  /* Get stats */
  ndb_mgm_get_tls_stats(mgmd.handle(), &stats[0]);
  print_stats(stats[0]);
  CHECK(ndb_mgm_has_tls(mgmd.handle()) == 0);  // Our handle does not use TLS,
  CHECK(stats[0].current > stats[0].tls);  // so current connections > TLS conns

  /* Now create a second client. It will use TLS */
  NdbMgmd client;
  client.use_tls(wd.path(), CLIENT_TLS_STRICT);
  CHECK(client.connect(mgmd.connectstring(config).c_str(), 1, 0));

  /* Get stats */
  ndb_mgm_get_tls_stats(mgmd.handle(), &stats[1]);
  print_stats(stats[1]);
  CHECK(stats[1].accepted > stats[0].accepted);
  CHECK(stats[1].upgraded > stats[0].upgraded);
  CHECK(stats[1].current > stats[0].current);
  CHECK(stats[1].tls > stats[0].tls);

  /* Wait for the MGMD cert to expire */
  client.disconnect();
  printf("Waiting 9 seconds for mgmd server certificate to expire.\n");
  sleep(9);

  /* Now a client will try to start TLS, and fail. */
  client.connect(mgmd.connectstring(config).c_str(), 1, 0);
  CHECK(client.last_error() == NDB_MGM_TLS_HANDSHAKE_FAILED);
  CHECK(client.is_connected() == false);
  client.close();

  /* The MGM server's TLS stats should reflect the failed attempt */
  ndb_mgm_get_tls_stats(mgmd.handle(), &stats[2]);
  print_stats(stats[2]);
  CHECK(stats[2].accepted > stats[1].accepted);
  CHECK(stats[2].upgraded == stats[1].upgraded);
  CHECK(stats[2].tls == stats[0].tls);
  CHECK(stats[2].current == stats[0].current);

  return NDBT_OK;
}

/* Test the TLS INFO statistics after the TLS auth has failed due to an
   expired client certificate
*/
int runTestTlsStats2(NDBT_Context *ctx, NDBT_Step *step) {
  ndb_mgm_tls_stats stats[2];
  auto print_stats = [](const ndb_mgm_tls_stats &stats) {
    printf("TLS Stats -- accepted:%d upgraded:%d current:%d tls:%d\n",
           stats.accepted, stats.upgraded, stats.current, stats.tls);
  };
  NDBT_Workingdir wd("test_tls");  // temporary working directory
  BaseString exe;
  NDBT_find_sign_keys(exe);

  /* Create a configuration */
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  /* Create certificates that will expire soon */
  CHECK(create_CA(wd, exe));
  CHECK(create_expiring_cert(wd, exe, "mgmd", "120"));
  CHECK(create_expiring_cert(wd, exe, "api", "5"));  // expires in 5 seconds

  /* MGM server */
  Mgmd mgmd(1);
  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());
  mgmdArgs.add("--ndb-tls-search-path=", wd.path());
  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  CHECK(mgmd.connect(config));             // Connect to management node
  CHECK(mgmd.wait_confirmed_config());     // Wait for configuration

  /* Get stats */
  ndb_mgm_get_tls_stats(mgmd.handle(), &stats[0]);
  print_stats(stats[0]);

  /* Create a client. Connect, but don't start TLS. */
  NdbMgmd client;
  TlsKeyManager tlsKeyManager;
  tlsKeyManager.init_mgm_client(wd.path());
  CHECK(client.connect(mgmd.connectstring(config).c_str(), 1, 0, false));
  CHECK(ndb_mgm_has_tls(client.handle()) == 0);

  /* Wait for the client cert to expire, then try to start TLS.
     The server's cert is valid, so the client will see auth as successful,
     but then it will fail on the next MGM call.
  */
  printf("Waiting 6 seconds for mgm client certificate to expire.\n");
  sleep(6);
  CHECK(client.start_tls(tlsKeyManager.ctx()) == 0);  // returns 0 on success
  CHECK(ndb_mgm_check_connection(client.handle()) == -1);

  /* Get stats */
  ndb_mgm_get_tls_stats(mgmd.handle(), &stats[1]);
  print_stats(stats[1]);
  CHECK(stats[1].accepted > stats[0].accepted);
  CHECK(stats[1].upgraded == stats[0].upgraded);
  CHECK(stats[1].tls == stats[0].tls);
  CHECK(stats[1].current == stats[0].current);

  return NDBT_OK;
}

int runTestRequireTls(NDBT_Context *ctx, NDBT_Step *step) {
  /* Create a configuration file in the working directory */
  NDBT_Workingdir wd("test_tls");
  BaseString cfg_path = path(wd.path(), "config.ini", nullptr);
  Properties config = ConfigFactory::create();
  ConfigFactory::put(config, "ndb_mgmd", 1, "RequireTls", "true");
  CHECK(ConfigFactory::write_config_ini(config, cfg_path.c_str()));

  /* Create keys in test_tls, and initialize our own TLS context */
  TlsKeyManager tls_km;
  bool k = sign_tls_keys(wd);
  CHECK(k);
  tls_km.init_mgm_client(wd.path());
  CHECK(tls_km.ctx());

  /* Start a management server that will require TLS */
  Mgmd mgmd(1);
  NdbProcess::Args mgmdArgs;
  mgmd.common_args(mgmdArgs, wd.path());
  mgmdArgs.add("--ndb-tls-search-path=", wd.path());
  CHECK(mgmd.start(wd.path(), mgmdArgs));  // Start management node
  sleep(1);                                // Wait for confirmed config

  /* Our management client */
  NdbMgmHandle handle = ndb_mgm_create_handle();
  ndb_mgm_set_connectstring(handle, mgmd.connectstring(config).c_str());
  ndb_mgm_set_ssl_ctx(handle, tls_km.ctx());

  int r = ndb_mgm_connect(handle, 3, 5, 1);  // Connect to management node
  CHECK(r == 0);

  ndb_mgm_severity sev = {NDB_MGM_EVENT_SEVERITY_ON, 1};
  r = ndb_mgm_get_clusterlog_severity_filter(handle, &sev, 1);
  CHECK(r < 1);  // COMMAND IS NOT YET ALLOWED
  int err = ndb_mgm_get_latest_error(handle);
  CHECK(err == NDB_MGM_AUTH_REQUIRES_TLS);

  struct ndb_mgm_cluster_state *st = ndb_mgm_get_status(handle);
  CHECK(st == nullptr);  // COMMAND IS NOT YET ALLOWED
  err = ndb_mgm_get_latest_error(handle);
  CHECK(err == NDB_MGM_AUTH_REQUIRES_TLS);

  r = ndb_mgm_start_tls(handle);
  printf("ndb_mgm_start_tls(): %d\n", r);  // START TLS
  CHECK(r == 0);

  r = ndb_mgm_get_clusterlog_severity_filter(handle, &sev, 1);
  CHECK(r == 1);  // NOW COMMAND IS ALLOWED

  return NDBT_OK;
}

NDBT_TESTSUITE(testTls);
DRIVER(DummyDriver); /* turn off use of NdbApi */

TESTCASE("NdbdWithoutCertificate",
         "Test data node startup with TLS required but no certificate"){
    INITIALIZER(runTestNdbdWithoutCert)}

TESTCASE("ApiWithoutCertificate",
         "Test API node without certificate where TRP TLS is required"){
    INITIALIZER(runTestApiWithoutCert)}

TESTCASE("NdbdWithExpiredCertificate",
         "Test data node startup with expired certificate"){
    INITIALIZER(runTestNdbdWithExpiredCert)}

TESTCASE("NdbdWithCertificate", "Test data node startup with certificate"){
    INITIALIZER(runTestNdbdWithCert)}

TESTCASE("StartTls", "Test START TLS in MGM protocol") {
  INITIALIZER(runTestStartTls);
}

TESTCASE("RequireTls", "Test MGM server that requires TLS") {
  INITIALIZER(runTestRequireTls);
}

TESTCASE("TlsStats1", "Test TLS INFO statistics after server cert expires") {
  INITIALIZER(runTestTlsStats1);
}

TESTCASE("TlsStats2", "Test TLS INFO statistics after client cert expires") {
  INITIALIZER(runTestTlsStats2);
}

TESTCASE("KeySigningTool", "Test key signing using a co-process tool") {
  INITIALIZER(runTestKeySigningTool);
}

TESTCASE("SshKeySigning",
         "Test remote key signing over ssh using ndb_sign_keys") {
  INITIALIZER(runTestSshKeySigning);
}

NDBT_TESTSUITE_END(testTls)

#endif

int main(int argc, const char **argv) {
  ndb_init();
  NDBT_TESTSUITE_INSTANCE(testTls);
  testTls.setCreateTable(false);
  testTls.setRunAllTables(true);
  testTls.setConnectCluster(false);
  testTls.setEnsureIndexStatTables(false);
  testTls.setCheckErrorInsert(false);

#ifdef NDB_USE_GET_ENV
  char buf1[255], buf2[255];
  if (NdbEnv_GetEnv("NDB_MGMD_VALGRIND_EXE", buf1, sizeof(buf1))) {
    exe_valgrind = buf1;
  }

  if (NdbEnv_GetEnv("NDB_MGMD_VALGRIND_ARG", buf2, sizeof(buf2))) {
    arg_valgrind = buf2;
  }
#endif

  return testTls.execute(argc, argv);
}
