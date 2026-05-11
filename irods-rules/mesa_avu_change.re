# mesa_avu_change.re
#
# iRODS Rule Language callback that mirrors AVU mutations into the
# project's DuckLake. Fires on ``acPostProcForModifyAVUMetadata`` and
# its sibling events (analogous AVU-change PEPs added in iRODS 4.3+).
#
# Wire contract: shell out to ``mesa-ducklake record`` with a single
# line of JSON on stdin. The CLI is installed system-wide by the
# mesa-ducklake admin (see README.md in this directory).
#
# This rule is intentionally minimal. It does not attempt to filter
# system-managed AVUs or to batch changes — the CLI is cheap enough,
# and the policy of "every AVU change is auditable" is a hard design
# constraint (see CLAUDE.md in mesa-ducklake).
#
# Variables iRODS provides at the PEP entry:
#   $userNameClient            - the iRODS user who made the change
#   $userZoneClient            - their zone
#   $ticketUserName            - set when the change went through a ticket
#   *Option                    - "add" | "rm" | "rmw" | ...
#   *ItemType                  - "-d" data, "-C" coll, "-R" resource, "-u" user
#   *ItemName                  - path or name of the target
#   *AttrName                  - attribute
#   *AttrValue                 - value
#   *AttrUnit                  - unit (may be empty)
#
# Some sites pass these via the ``args`` list instead of bind variables.
# The rule below uses the args form because it's the wire shape the
# iRODS 4.3 PEP uses; older sites should adapt.

acPostProcForModifyAVUMetadata(*Option, *ItemType, *ItemName, *AttrName, *AttrValue, *AttrUnit) {
    # Skip rmw / rmi (removes by index): the AVU is gone before we can
    # observe its old value cleanly. We still record the op as "delete"
    # so the snapshot history stays consistent; downstream tooling can
    # reconcile.
    *op = "add";
    if (*Option == "rm" || *Option == "rmw" || *Option == "rmi") {
        *op = "delete";
    }
    if (*Option != "add" && *Option != "rm" && *Option != "rmw" && *Option != "rmi") {
        # mod / set / ... — treat as add for the new triple. The catalog
        # row for the *old* triple is recorded separately by iRODS via
        # the implicit delete that precedes a mod.
        *op = "add";
    }

    # Map the ItemType marker to mesa-ducklake's target_type vocabulary.
    *target_type = "data_object";
    if (*ItemType == "-C") { *target_type = "collection"; }
    if (*ItemType == "-R") { *target_type = "resource"; }
    if (*ItemType == "-u") { *target_type = "user"; }

    # Capture ticket id if the operation came through a ticket.
    *via_ticket = "";
    if (errorcode(msiGetSystemTimeInDate("*ticketUserName", "*tNow")) == 0) {
        # If $ticketUserName is set, we forward it as the via_ticket id.
        # Different iRODS versions expose this differently; on most sites
        # the variable is available as $ticketUserName.
        *via_ticket = "$ticketUserName";
    }

    # Build the JSON payload. msiStrCat is used for portability across
    # iRL versions; iRODS 4.3 also exposes msiStrToBytesBuf which would
    # let us stream-pipe to the CLI, but msiExecCmd's stdin tunneling is
    # widely supported and good enough.
    *json = "";
    msiStrCat("*json", "{");
    msiStrCat("*json", '"irods_path":"');
    msiStrCat("*json", "*ItemName");
    msiStrCat("*json", '","target_type":"');
    msiStrCat("*json", "*target_type");
    msiStrCat("*json", '","attribute":"');
    msiStrCat("*json", "*AttrName");
    msiStrCat("*json", '","value":"');
    msiStrCat("*json", "*AttrValue");
    msiStrCat("*json", '","unit":"');
    msiStrCat("*json", "*AttrUnit");
    msiStrCat("*json", '","op":"');
    msiStrCat("*json", "*op");
    msiStrCat("*json", '","actor":"$userNameClient","source":"irods-rule:acPostProcForModifyAVUMetadata","rule_invocation":"mesa_avu_change"');
    if (*via_ticket != "") {
        msiStrCat("*json", ',"via_ticket":"');
        msiStrCat("*json", "*via_ticket");
        msiStrCat("*json", '"');
    }
    msiStrCat("*json", "}");

    # Shell out to the CLI. msiExecCmd takes (cmd, args, host, ...,
    # *Out). The stdin tunnel form passes our JSON to the CLI:
    *args = "record";
    *cmdOut = "";
    msiExecCmd("mesa-ducklake-record-wrapper.sh", "*args", "null", "null", "*json", *cmdOut);

    # The wrapper script (see irods-rules/README.md) sets MESA_DUCKLAKE_DSN
    # and execs ``mesa-ducklake record``. We do not branch on the exit
    # code: a non-zero exit (exit 2 for "not MESA-enabled" especially)
    # is the documented "silent skip" case and should not abort the
    # AVU operation itself.
}

acPostProcForModifyAVUMetadata(*Option, *ItemType, *ItemName, *AttrName, *AttrValue) {
    # 5-arg overload (no unit). Re-enter the 6-arg form with an empty unit.
    acPostProcForModifyAVUMetadata(*Option, *ItemType, *ItemName, *AttrName, *AttrValue, "");
}
