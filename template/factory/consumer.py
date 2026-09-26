"""Invoke a pinned, complete Archon source installation. No stage policy lives here."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "pack.json").read_text(encoding="utf-8"))
SETTINGS = ".factory/consumer.json"
ENTRY = "packages/cli/src/cli.ts"
EXPECTED_ARCHON_REVISION_FLAG = "--expected-archon-revision"
CODE_INTELLIGENCE_MODES = frozenset({"off", "optional", "required"})
DEFAULT_CODE_INTELLIGENCE = {"mode": "off"}
CODEGRAPH_MANAGED_RESOURCE = "codegraph_managed_v1"
CODEGRAPH_RUN_FLAG = "--codegraph"
FACTORY_OWNED_CAPABILITIES = {
    "run": {EXPECTED_ARCHON_REVISION_FLAG},
}


def code_intelligence_mode(settings: dict) -> str:
    """Return the operator-owned policy without accepting executable configuration."""
    policy = settings.get("code_intelligence")
    if policy is None:
        # Existing installations predate the consent field. They remain byte-compatible
        # and, critically, never gain indexing or a new MCP server during an upgrade.
        return "off"
    if not isinstance(policy, dict) or set(policy) != {"mode"}:
        raise ValueError("code_intelligence must contain only a mode field")
    mode = policy.get("mode")
    if mode not in CODE_INTELLIGENCE_MODES:
        raise ValueError("code_intelligence.mode must be off, optional, or required")
    return mode


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("Consumer settings must be a JSON object")
    code_intelligence_mode(settings)
    return settings


def write_settings(root: Path, settings: dict) -> None:
    """Atomically write machine-local settings in the shared Git root."""
    settings = validate_settings(settings)
    path = shared_root(root) / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                           dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(settings, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copystat(path, temporary)
        os.replace(temporary, path)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def execute(argv: list[str], cwd: Path, *, capture: bool = True,
            timeout: int | None = 180, env: dict | None = None) -> subprocess.CompletedProcess:
    # Resolve PATH and PATHEXT once, including Windows .cmd launchers.
    argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    return subprocess.run(argv, cwd=cwd, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def checked(argv: list[str], cwd: Path, timeout: int = 180) -> str:
    result = execute(argv, cwd, timeout=timeout)
    if result.returncode:
        raise ValueError(f"Command exited {result.returncode}: {argv[:3]}\n"
                         + (result.stderr or result.stdout)[-3000:])
    return result.stdout


def project_root() -> Path:
    return Path(checked(["git", "rev-parse", "--show-toplevel"], Path.cwd()).strip()).resolve()


def shared_root(root: Path) -> Path:
    common = checked(["git", "rev-parse", "--git-common-dir"], root).strip()
    return (root / common).resolve().parent


def read_settings(root: Path) -> dict:
    path = shared_root(root) / SETTINGS
    if not path.is_file():
        raise ValueError("Integration pin required. Run factory init --source <complete Archon "
                         "checkout or URL> --revision <40-character SHA> --cache <directory>.")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        return validate_settings(settings)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Invalid consumer settings: {path}: {error}") from error


def expected_archon_revision(action: str, args: list[str]) -> tuple[str | None, list[str]]:
    """Consume Factory's exact-revision assertion without forwarding it to Archon."""
    matches = [index for index, arg in enumerate(args)
               if arg == EXPECTED_ARCHON_REVISION_FLAG
               or arg.startswith(EXPECTED_ARCHON_REVISION_FLAG + "=")]
    if not matches:
        return None, list(args)
    if action != "run":
        raise ValueError(f"{EXPECTED_ARCHON_REVISION_FLAG} supports factory run only")
    if len(matches) != 1:
        raise ValueError(f"{EXPECTED_ARCHON_REVISION_FLAG} must be supplied exactly once")
    index = matches[0]
    boundary = args.index("--") if "--" in args else len(args)
    if index >= boundary:
        raise ValueError(f"{EXPECTED_ARCHON_REVISION_FLAG} must precede --")
    if args[index] != EXPECTED_ARCHON_REVISION_FLAG:
        raise ValueError(
            f"{EXPECTED_ARCHON_REVISION_FLAG} requires the separate form: "
            f"{EXPECTED_ARCHON_REVISION_FLAG} <40-character SHA>"
        )
    if index + 1 >= boundary:
        raise ValueError(f"{EXPECTED_ARCHON_REVISION_FLAG} requires a 40-character SHA")
    revision = args[index + 1]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"{EXPECTED_ARCHON_REVISION_FLAG} requires a full lowercase commit SHA")
    return revision, [*args[:index], *args[index + 2:]]


def require_expected_archon_revision(settings: dict, expected: str) -> None:
    actual = settings.get("revision")
    if actual != expected:
        raise ValueError(
            f"Pinned Archon revision mismatch: expected {expected}, found {actual!r}"
        )


def verify_source(settings: dict) -> Path:
    revision = settings.get("revision", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Integration revision must be a full lowercase commit SHA")
    source = Path(settings["source"]).resolve()
    if source.name != revision:
        raise ValueError("Source must use an immutable per-pin directory named with its full SHA")
    actual = checked(["git", "rev-parse", "HEAD"], source).strip()
    if actual != revision:
        raise ValueError(f"Source SHA mismatch: expected {revision}, found {actual}")
    dirty = checked(["git", "status", "--porcelain", "--untracked-files=all"], source)
    if dirty.strip():
        # Name the paths and the way back. The usual cause is a dependency install
        # inside the pinned tree, which looks harmless and stops every run.
        changed = [line[3:].strip() for line in dirty.strip().splitlines() if line[3:].strip()]
        shown = ", ".join(changed[:5])
        if len(changed) > 5:
            shown += f", and {len(changed) - 5} more"
        raise ValueError(
            f"Pinned source has changes ({shown}). The pinned tree is verified byte for byte. "
            f"Restore it with: rm -rf {source} && python3 bin/factory.py init from your repo. "
            f"To change engine behavior, move the pin in factory/pack.json instead of editing the tree"
        )
    # Include ignored files under authoring roots: an ignored workflow can shadow a
    # committed name just as an untracked one can. Runtime dependencies stay ignored.
    tracked = set(checked(["git", "ls-files"], source).splitlines())
    for directory in (source / ".archon", source / "packages/cli/src"):
        if directory.is_symlink() or not directory.resolve().is_relative_to(source):
            raise ValueError(f"Source authoring directory escapes the pin: {directory}")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Source authoring symlink is not supported: {path}")
            if path.is_file() and path.relative_to(source).as_posix() not in tracked:
                raise ValueError(f"Untracked authoring file in pinned source: {path}")
    for rel in (ENTRY, "package.json", "bun.lock", MANIFEST["source_directory"]):
        if not (source / rel).exists():
            raise ValueError(f"Incomplete Archon source: missing {rel}")
    if not (source / "node_modules").is_dir():
        raise ValueError("Archon dependencies missing; complete the pinned source installation")
    return source


def cli(settings: dict, source: Path) -> list[str]:
    # Never use an ambient archon binary or provider command override.
    return [settings.get("bun", "bun"), str(source / ENTRY)]


def native_json(settings: dict, source: Path, args: list[str], cwd: Path) -> dict:
    raw = checked([*cli(settings, source), *args, "--cwd", str(cwd), "--json"], cwd)
    data = json.loads(raw)  # One whole document, including pretty-printed JSON.
    if not isinstance(data, dict) or data.get("ok") is False:
        raise ValueError(f"Invalid native response: {raw[:1000]}")
    return data


def source_workflows(source: Path) -> dict[str, Path]:
    # This is only a provenance index. Archon parses and validates the definitions.
    found = {}
    for path in (source / MANIFEST["source_directory"]).rglob("*"):
        if path.suffix not in (".yaml", ".yml"):
            continue
        match = re.search(r"^name:\s*['\"]?([a-z][a-z0-9-]*)['\"]?\s*$",
                          path.read_text(encoding="utf-8"), re.M)
        if match:
            name = match[1]
            if name in found:
                raise ValueError(f"Duplicate source workflow: {name}")
            found[name] = path
    # A second project definition must not shadow a selected pack member.
    for path in (source / ".archon/workflows").rglob("*"):
        if path.suffix not in (".yaml", ".yml") or path in found.values():
            continue
        match = re.search(r"^name:\s*['\"]?([a-z][a-z0-9-]*)['\"]?\s*$",
                          path.read_text(encoding="utf-8"), re.M)
        if match and match[1] in found:
            raise ValueError(f"Conflicting workflow outside the SDLC pack: {path}")
    # Packaged resources are resolved relative to their author's package by
    # Archon. Require the literal references to exist there, so a home-scoped or
    # bundled fallback cannot make an incomplete source look installable. Native
    # validation below remains responsible for parsing, types and the graph.
    for name, path in found.items():
        for kind, value in re.findall(r"^\s+(command|script|include):\s*([^\n#]+)",
                                      path.read_text(encoding="utf-8"), re.M):
            value = value.strip().strip("'\"")
            # Inline script code is already part of this pinned YAML. Match the
            # native isInlineScript rule; it has no external file to resolve.
            if kind == "script" and re.search(r"[;(){}&|<>$`\"' ]", value):
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_./-]+", value):
                raise ValueError(f"Cannot verify nonliteral {kind} provenance in {name}: {value}")
            if kind == "include":
                if value not in found:
                    raise ValueError(f"Include {value} is missing from pinned SDLC source")
                continue
            directory = path.parent / ("commands" if kind == "command" else "scripts")
            candidates = [directory / (value + ".md")] if kind == "command" else [
                directory / (value + suffix) for suffix in ("", ".py", ".ts", ".js", ".sh")]
            if not any(p.is_file() and p.resolve().is_relative_to(source) for p in candidates):
                raise ValueError(f"Missing source-owned {kind} {value} for {name}")
    return found


def discover(settings: dict, source: Path) -> dict[str, Path]:
    local = source_workflows(source)
    data = native_json(settings, source, ["workflow", "list"], source)
    if data.get("errors"):
        raise ValueError(f"Native workflow discovery errors: {data['errors']}")
    rows = data.get("workflows")
    if not isinstance(rows, list):
        raise ValueError("Native workflow list returned no workflows array")
    names = {row["name"] for row in rows}
    return {name: path for name, path in local.items() if name in names}


def validate(settings: dict, source: Path, name: str) -> None:
    data = native_json(settings, source, ["validate", "workflows", name], source)
    results = data.get("results", [])
    if (not results or any(row.get("valid") is not True for row in results)
            or data.get("summary", {}).get("errors", 0)):
        raise ValueError(f"Workflow/command validation failed for {name}: {data}")


def engine_contract(settings: dict, source: Path) -> dict:
    raw = checked([*cli(settings, source), "version", "--json"], source)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Pinned CLI version contract is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Pinned CLI version contract must be a JSON object")
    if data.get("revision") != settings["revision"]:
        raise ValueError(
            "Pinned CLI revision declaration mismatch: "
            f"expected {settings['revision']}, found {data.get('revision')!r}"
        )
    declared = data.get("contracts")
    if declared != MANIFEST["contracts"]:
        raise ValueError(
            "Pinned CLI contract declaration mismatch: "
            f"expected {MANIFEST['contracts']}, found {declared!r}"
        )
    return data


def validate_engine_contract(settings: dict, source: Path) -> None:
    engine_contract(settings, source)


def code_intelligence_support(settings: dict, source: Path) -> bool:
    capabilities = engine_contract(settings, source).get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(item, str)
                                                  for item in capabilities):
        raise ValueError("Pinned CLI capabilities must be an array of strings")
    return CODEGRAPH_MANAGED_RESOURCE in capabilities


def code_intelligence_registry(settings: dict, source: Path) -> dict:
    """Read Archon's non-secret registry check without invoking the adapter."""
    result = execute([*cli(settings, source), "doctor", "--json"], source)
    if result.returncode not in {0, 1}:
        raise ValueError(f"Pinned CLI doctor exited unexpectedly with {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Pinned CLI doctor contract is not valid JSON") from error
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError("Pinned CLI doctor returned no checks array")
    rows = [row for row in checks
            if isinstance(row, dict) and row.get("id") == CODEGRAPH_MANAGED_RESOURCE]
    if len(rows) != 1:
        raise ValueError("Pinned CLI doctor returned no unique codegraph_managed_v1 check")
    row = rows[0]
    if row.get("status") not in {"pass", "skip", "fail"}:
        raise ValueError("Pinned CLI doctor returned invalid status for codegraph_managed_v1")
    for key in ("configured", "ready"):
        # bool is deliberately checked by type: Python otherwise treats 0/1 as
        # members of {False, True}, weakening the native JSON contract.
        if not isinstance(row.get(key), bool):
            raise ValueError(f"Pinned CLI doctor returned invalid {key} for codegraph_managed_v1")
    # A pass is the only state that proves a usable operator registry. Skip is the
    # honest default-off state; fail is invalid configuration, never availability.
    if row["status"] == "pass":
        if row.get("schemaVersion") != 1 or row.get("protocol") != "codegraph_worktree_adapter_v1":
            raise ValueError("Pinned CLI doctor returned an incompatible codegraph_managed_v1 schema")
        if not isinstance(row.get("expectedVersion"), str) or not row["expectedVersion"]:
            raise ValueError("Pinned CLI doctor omitted the managed CodeGraph version")
        if not isinstance(row.get("contractSha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", row["contractSha256"]):
            raise ValueError("Pinned CLI doctor returned an invalid managed-resource contract digest")
        if not (row["configured"] and row["ready"]):
            raise ValueError("Pinned CLI doctor contradicted its codegraph_managed_v1 pass status")
    if row["status"] == "skip" and (row["configured"] or row["ready"]):
        raise ValueError("Pinned CLI doctor contradicted its codegraph_managed_v1 skip status")
    if row["status"] == "fail" and (not row["configured"] or row["ready"]):
        raise ValueError("Pinned CLI doctor contradicted its codegraph_managed_v1 fail status")
    return row


def doctor(settings: dict) -> dict:
    source = verify_source(settings)
    mode = code_intelligence_mode(settings)
    supported = code_intelligence_support(settings, source)
    registry = None
    if supported:
        registry = code_intelligence_registry(settings, source)
    if mode != "off" and not supported:
        raise ValueError("Pinned Archon does not support codegraph_managed_v1")
    if mode == "required" and registry and registry["status"] != "pass":
        raise ValueError("CodeGraph is required but the Archon codegraph_managed_v1 registry is not ready")
    # An invalid or absent registry is diagnostic for optional mode: Archon's
    # sealed run policy owns the observable raw-navigation fallback. Only the
    # operator's required mode is fail-closed before provider spend.
    # Global flags such as --verbose live in the root help, while --json may appear
    # only in examples and --events is owned by the workflow subcommand. A flag
    # counts as documented at any of those native help levels; the subcommand
    # itself must still exist.
    global_help = checked([*cli(settings, source), "--help"], source)
    shared_help = checked([*cli(settings, source), "workflow", "--help"], source)
    for command, flags in MANIFEST["capabilities"].items():
        factory_owned = FACTORY_OWNED_CAPABILITIES.get(command, set())
        missing_factory = factory_owned - set(flags)
        if missing_factory:
            raise ValueError(
                f"Factory manifest missing consumer-owned {command} capability: "
                f"{sorted(missing_factory)}"
            )
        help_text = checked([*cli(settings, source), "workflow", command, "--help"], source)
        if f"workflow {command}" not in help_text or any(
                flag not in factory_owned
                and flag not in help_text and flag not in shared_help and flag not in global_help
                for flag in flags):
            raise ValueError(f"Pinned CLI missing workflow {command} capability: {flags}")
    discovered = discover(settings, source)
    missing = sorted(set(MANIFEST["entries"]) - discovered.keys())
    if missing:
        raise ValueError("Incomplete integration source; missing shared workflows: " + ", ".join(missing))
    for name in discovered:
        validate(settings, source, name)
    return {"source": str(source), "revision": settings["revision"],
            "workflows": sorted(discovered), "automation": "supervised integration",
            "provider_configuration": "native configuration preserved; authentication not live-tested",
            "code_intelligence": {
                "mode": mode,
                "resource": CODEGRAPH_MANAGED_RESOURCE,
                "engine_supported": supported,
                "registry_status": registry["status"] if registry else "unsupported",
                "registry_configured": registry["configured"] if registry else False,
                "registry_ready": registry["ready"] if registry else False,
            }}


RETIRED = {
    "accept": "Use factory approve/respond <run-id> for an actual declared Archon gate.",
    "level": "The autonomy dial is retired and cannot authorize work or merges.",
    "arm": "Use an OS timer to invoke factory tick; see the README.",
    "disarm": "Remove the old factory cron/Task Scheduler entries explicitly. Use cancel <run-id> for native runs.",
    "merge": "Use a shared queue workflow when present in the integration source; its gate owns merge authorization.",
    "deploy": "Move deployment into a shared release workflow with an explicit gate.",
    "fix": "Use factory run archon-deliver --adopt <run-id> --input work=<findings file>.",
    "implement": "Use factory run archon-ship --input target=<request>.",
    "triage": "Use factory run archon-triage --input target=<request>.",
    "validate": "Use factory run archon-validate with the producer's declared inputs.",
    "regress": "Use a shared regression workflow when present in the pinned source.",
}


def refuse(action: str) -> int:
    print(f"Retired factory operation '{action}'. " + RETIRED.get(action,
          "Use factory run <shared-workflow> or native status/get/cancel/resume."), file=sys.stderr)
    return 2


def with_default_inputs(name: str, args: list[str], options: list[str]) -> list[str]:
    supplied = set()
    for index, arg in enumerate(options):
        if arg == "--input" and index + 1 < len(options):
            supplied.add(options[index + 1].split("=", 1)[0])
        elif arg.startswith("--input="):
            supplied.add(arg.removeprefix("--input=").split("=", 1)[0])
    defaults = MANIFEST.get("default_inputs", {}).get(name, {})
    injected = [item for key, value in defaults.items() if key not in supplied
                for item in ("--input", f"{key}={value}")]
    return [args[0], *injected, *args[1:]]


def configure_code_intelligence(root: Path, args: list[str]) -> int:
    if not args:
        raise ValueError(
            "Usage: factory code-intelligence enable --mode optional|required, or disable"
        )
    operation = args[0]
    if operation == "disable":
        if len(args) != 1:
            raise ValueError("factory code-intelligence disable takes no arguments")
        settings = read_settings(root)
        updated = {**settings, "code_intelligence": {"mode": "off"}}
        write_settings(root, updated)
        print(json.dumps({"code_intelligence": {"mode": "off"}}, indent=2))
        return 0
    if operation != "enable" or len(args) != 3 or args[1] != "--mode":
        raise ValueError(
            "Usage: factory code-intelligence enable --mode optional|required, or disable"
        )
    mode = args[2]
    if mode not in {"optional", "required"}:
        raise ValueError("enable mode must be optional or required; use disable for off")
    settings = read_settings(root)
    source = verify_source(settings)
    if not code_intelligence_support(settings, source):
        raise ValueError("Pinned Archon does not support codegraph_managed_v1")
    registry = code_intelligence_registry(settings, source)
    if mode == "required" and registry["status"] != "pass":
        raise ValueError(
            "CodeGraph required mode needs a ready operator-owned codegraph_managed_v1 registry"
        )
    updated = {**settings, "code_intelligence": {"mode": mode}}
    write_settings(root, updated)
    print(json.dumps({"code_intelligence": {
        "mode": mode,
        "resource": CODEGRAPH_MANAGED_RESOURCE,
        "registry_status": registry["status"],
        "registry_ready": registry["ready"],
    }}, indent=2))
    return 0


def invoke(root: Path, action: str, args: list[str]) -> int:
    args = list(args)
    if action == "code-intelligence":
        return configure_code_intelligence(root, args)
    expected_revision, args = expected_archon_revision(action, args)
    runtime_config = None
    sealed_code_intelligence_mode = None
    options = args[:args.index("--")] if "--" in args else args[:]
    runtime_flags = [a for a in options if a.split("=", 1)[0] == "--runtime-host"]
    if runtime_flags:
        if action != "run" or len(runtime_flags) != 1:
            raise ValueError("--runtime-host supports one foreground run only")
        if any(a.split("=", 1)[0] in {"--detach", "--resume", "-d"} for a in options):
            raise ValueError("Detached/resumed runtime-host mode is unsupported: no public durable ownership contract; use a new foreground run")
        flag = runtime_flags[0]
        index = args.index(flag)
        if "=" in flag:
            runtime_config = flag.split("=", 1)[1]
            del args[index]
        else:
            if index + 1 >= len(options) or options[index + 1].startswith("--"):
                raise ValueError("--runtime-host requires a trusted project configuration path")
            runtime_config = args.pop(index + 1)
            args.pop(index)
        if not runtime_config:
            raise ValueError("--runtime-host requires a configuration path")
    if action == "tick":
        if args:
            raise ValueError("tick takes no arguments; configure .factory/schedule.json")
        schedule = json.loads((shared_root(root) / ".factory/schedule.json").read_text(encoding="utf-8"))
        workflow = schedule.get("workflow", "archon-lifecycle")
        inputs = schedule.get("inputs")
        if not isinstance(workflow, str) or not isinstance(inputs, dict):
            raise ValueError("schedule.json requires a shared workflow and inputs object")
        args = [workflow]
        for key, value in inputs.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("Invalid scheduled workflow input name")
            args += ["--input", key + "=" + (value if isinstance(value, str) else json.dumps(value))]
        if "runtime_host" in schedule:
            host = schedule["runtime_host"]
            if not isinstance(host, str) or not host:
                raise ValueError("runtime_host must be a non-empty configuration path")
            args += ["--runtime-host", host]
        # Scheduling submits exactly one shared workflow, never individual stages.
        return invoke(root, "run", args)
    if action in RETIRED:
        return refuse(action)
    if action not in {"run", "list", "get", "status", "approve", "reject", "respond",
                      "cancel", "resume", "doctor", "halt", "unhalt"}:
        return refuse(action)
    stop = shared_root(root) / ".factory/STOP"
    if action in {"halt", "unhalt"}:
        if args:
            raise ValueError(f"{action} takes no arguments. Cancel an active run by its run ID.")
        if action == "halt":
            stop.parent.mkdir(parents=True, exist_ok=True)
            stop.write_text("Operator stopped new factory launches.\n", encoding="utf-8")
        else:
            stop.unlink(missing_ok=True)
        print("Local launch brake " + ("set. Active runs require cancel <run-id>." if action == "halt" else "cleared."))
        return 0
    for arg in args:
        if arg.split("=", 1)[0] in {"--cwd", "--workflow-source", CODEGRAPH_RUN_FLAG}:
            raise ValueError(
                "Factory owns --cwd, --workflow-source, and --codegraph; "
                "select CodeGraph only with factory code-intelligence"
            )
    if action in {"run", "resume", "approve", "respond"} and stop.exists():
        raise ValueError("Local STOP is set. Use unhalt to permit launch/continuation; cancel remains available")
    settings = read_settings(root)
    if expected_revision is not None:
        require_expected_archon_revision(settings, expected_revision)
    source = verify_source(settings)
    if action == "doctor":
        print(json.dumps(doctor(settings), indent=2))
        return 0
    if action == "list":
        print(json.dumps({"source": str(source), "revision": settings["revision"],
                          "workflows": sorted(discover(settings, source))}, indent=2))
        return 0
    # These flags must precede caller arguments. Appending after a caller's `--`
    # turns them into message text and silently restores ambient source discovery.
    native = ["workflow", action, "--cwd", str(root)]
    if action == "run":
        if not args:
            raise ValueError("Usage: factory run <shared-workflow> [native options and message]")
        name = args[0]
        if name in RETIRED:
            return refuse(name)
        if name not in discover(settings, source):
            raise ValueError(f"Shared workflow '{name}' is absent from the pinned SDLC source; no fallback")
        validate(settings, source, name)
        options = args[:args.index("--")] if "--" in args else args
        resuming = any(arg.split("=", 1)[0] == "--resume" for arg in options)
        if not resuming:
            native += ["--workflow-source", str(source)]
            mode = code_intelligence_mode(settings)
            sealed_code_intelligence_mode = mode
            supported = code_intelligence_support(settings, source)
            if mode != "off" and not supported:
                raise ValueError("Pinned Archon does not support codegraph_managed_v1")
            if supported:
                if mode == "required":
                    registry = code_intelligence_registry(settings, source)
                    if registry["status"] != "pass":
                        raise ValueError(
                            "CodeGraph is required but the codegraph_managed_v1 registry is not ready"
                        )
                native += [CODEGRAPH_RUN_FLAG, mode]
            args = with_default_inputs(name, args, options)
    native += args
    if action == "status":
        print(f"Factory source={source} revision={settings['revision']} local_STOP={stop.exists()}", file=sys.stderr)
    # Native output, exit code, inputs, identity and gates pass through unchanged.
    # No subprocess deadline or retry can guess whether a native run is alive.
    def launch(env: dict | None = None) -> int:
        launch_settings, launch_source = settings, source
        launch_native = list(native)
        if sealed_code_intelligence_mode is not None:
            latest_settings = read_settings(root)
            if code_intelligence_mode(latest_settings) != sealed_code_intelligence_mode:
                raise ValueError(
                    "Code-intelligence consent changed during preflight; retry the run"
                )
        if expected_revision is not None:
            # Re-read the git-common-dir settings at the last possible point. A
            # linked worktree's local .factory file is not the consumer authority,
            # and a pin changed after preflight must never reach the native spawn.
            launch_settings = read_settings(root)
            require_expected_archon_revision(launch_settings, expected_revision)
            launch_source = verify_source(launch_settings)
            if "--workflow-source" in launch_native:
                source_index = launch_native.index("--workflow-source") + 1
                launch_native[source_index] = str(launch_source)
        return execute([*cli(launch_settings, launch_source), *launch_native], root,
                       capture=False, timeout=None, env=env).returncode

    if runtime_config:
        from runtime_host import RuntimeHost
        with RuntimeHost(root / runtime_config) as host:
            return launch({**os.environ, **host.environment()})
    return launch()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"--help", "-h"}:
        print("factory run <shared-workflow> [native arguments]\n"
              "factory run <shared-workflow> --expected-archon-revision <SHA> [native arguments]\n"
              "factory run <shared-workflow> --runtime-host <config.json> [foreground native arguments]\n"
              "Runtime host: fresh ordinary apps; detach/resume unsupported. Manual: python factory/runtime_host.py serve --help\n"
              "factory tick (one scheduled shared workflow, foreground)\n"
              "factory code-intelligence enable --mode optional|required | disable\n"
              "factory list | doctor | status | get <run-id>\n"
              "factory approve | reject | respond | cancel | resume <run-id>\n"
              "factory halt | unhalt (local launch brake only)")
        return 0
    try:
        return invoke(project_root(), args[0], args[1:])
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"Factory refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
