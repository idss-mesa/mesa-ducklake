# mesa_enroll_policy.re
#
# Auto-enrol new iRODS collections into MESA when their parent carries
# the ``mesa.auto_enroll=true`` AVU. Fires on
# ``acPostProcForCollCreate`` (and analogous PEPs added in iRODS 4.3+).
#
# Effect: when a collection is created at <parent>/<child>, this rule
# checks the parent for ``mesa.auto_enroll=true``. If present, it
# stamps the new child with ``mesa.enabled=true`` and creates the
# ``<child>/.mesa/ducklake`` subcollection. The next AVU change on the
# child (or anywhere under it) is then caught by mesa_avu_change.re.
#
# The actual project registration in the Postgres catalog happens the
# first time an AVU change reaches mesa-ducklake — the CLI's
# ``mesa-ducklake record`` walks up looking for a project, and falls
# through to "not MESA-enabled" when none exists. To register a project
# proactively, set a delayed task via mesa-mcp's
# mesa_ducklake_init_project tool — that's the documented bootstrap
# for explicit enrolment.

acPostProcForCollCreate {
    *coll = $collName;
    *parent = trimr("*coll", "/");
    if (*parent == "*coll" || *parent == "") {
        # Top-level collection; nothing to inherit.
        succeed;
    }

    # Check the parent for the auto-enroll AVU.
    *parent_auto_enroll = "false";
    *qstring = "SELECT META_COLL_ATTR_VALUE WHERE COLL_NAME = '*parent' AND META_COLL_ATTR_NAME = 'mesa.auto_enroll'";
    foreach (*row in *qstring) {
        *parent_auto_enroll = *row.META_COLL_ATTR_VALUE;
    }
    if (*parent_auto_enroll != "true") {
        succeed;
    }

    # Inherit the MESA marker AVU.
    msiAddKeyVal(*kv, "mesa.enabled", "true");
    msiAssociateKeyValuePairsToObj(*kv, "*coll", "-C");

    # Create the ``/.mesa/ducklake`` data-file directory.
    *mesa_dir = "*coll/.mesa";
    *ducklake_dir = "*coll/.mesa/ducklake";
    msiCollCreate("*mesa_dir", "1", *err);
    msiCollCreate("*ducklake_dir", "1", *err);

    # Optionally notify mesa-ducklake so it can pre-register the project
    # in the Postgres catalog. We pipe a small JSON to ``mesa-ducklake
    # record`` with a synthetic AVU change describing the enrolment.
    # This is best-effort; the CLI returns 2 (not_mesa_enabled) until
    # the project is registered, after which subsequent changes flow.
    *enroll_json = "";
    msiStrCat("*enroll_json", "{");
    msiStrCat("*enroll_json", '"irods_path":"');
    msiStrCat("*enroll_json", "*coll");
    msiStrCat("*enroll_json", '","target_type":"collection","attribute":"mesa.enabled","value":"true","unit":"","op":"add","actor":"$userNameClient","source":"irods-rule:acPostProcForCollCreate","rule_invocation":"mesa_enroll_policy"}');
    *args = "record";
    *cmdOut = "";
    msiExecCmd("mesa-ducklake-record-wrapper.sh", "*args", "null", "null", "*enroll_json", *cmdOut);
}
