from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
PLUGIN_DIR = BASE_DIR / "lightroom" / "AIPhotoAgent.lrplugin"
DEFAULT_TOOLS_DIR = BASE_DIR / "data" / "runtime" / "tools"
REQUIRED_ACTIONS = (
    "applyDevelopSettings",
    "createDevelopSnapshot",
    "createVirtualCopy",
    'switchToModule("develop")',
    "requestJpegThumbnail",
)
REQUIRED_HTTP = (
    "postMultipart",
    "LrHttp.post",
)


def _load_luaparser(tools_dir: Path):
    if tools_dir.is_dir() and str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from luaparser import ast  # type: ignore
    except ImportError:
        return None
    return ast


def validate_syntax(luaparser_module) -> dict:
    files = sorted(PLUGIN_DIR.glob("*.lua"))
    if luaparser_module is None:
        return {
            "checked": False,
            "files": [file.name for file in files],
            "message": "luaparser is not installed; syntax check skipped.",
        }
    failures = []
    for file in files:
        try:
            luaparser_module.parse(file.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - diagnostic branch
            failures.append({"file": file.name, "error": str(exc)})
    return {
        "checked": True,
        "files": [file.name for file in files],
        "failures": failures,
        "ok": not failures,
    }


def validate_package() -> dict:
    info_path = PLUGIN_DIR / "Info.lua"
    if not info_path.is_file():
        return {"ok": False, "message": "Info.lua is missing."}
    info = info_path.read_text(encoding="utf-8")
    menu_files = re.findall(r'file\s*=\s*"([^"]+)"', info)
    missing_menu_files = [
        name for name in menu_files if not (PLUGIN_DIR / name).is_file()
    ]
    actions = (PLUGIN_DIR / "Actions.lua").read_text(encoding="utf-8")
    http = (PLUGIN_DIR / "Http.lua").read_text(encoding="utf-8")
    missing_actions = [name for name in REQUIRED_ACTIONS if name not in actions]
    missing_http = [name for name in REQUIRED_HTTP if name not in http]
    return {
        "ok": not (missing_menu_files or missing_actions or missing_http),
        "plugin_dir": str(PLUGIN_DIR),
        "menu_files": menu_files,
        "missing_menu_files": missing_menu_files,
        "missing_actions": missing_actions,
        "missing_http": missing_http,
        "has_init_plugin": "LrInitPlugin" in info,
        "has_library_menu": "LrLibraryMenuItems" in info,
    }


def validate_bridge_runtime() -> dict:
    try:
        from lupa import LuaRuntime  # type: ignore
    except ImportError:
        return {
            "checked": False,
            "message": "lupa is not installed; bridge runtime check skipped.",
        }

    job = {
        "schema_version": "aipa_lightroom_job_v1",
        "job_id": "job_validator",
        "style": {"id": "natural-documentary", "name": "自然纪实"},
        "preset_name": "AI Photo Agent - 自然纪实 - 70",
        "photo": {"photo_id": "photo_validator", "original_filename": "validator.jpg"},
        "develop_settings": {
            "ProcessVersion": "15.4",
            "Exposure2012": 0.2,
            "ToneCurvePV2012": [0, 0, 64, 60, 128, 128, 255, 255],
        },
    }
    plugin_path = str(PLUGIN_DIR).replace("\\", "/")
    temp_root = Path(tempfile.mkdtemp(prefix="aipa-validator-"))
    try:
        lightroom_root = temp_root / "lightroom"
        bridge_dir = lightroom_root / "AI Photo Agent Bridge"
        for name in ("inbox", "done", "failed", "processing"):
            (bridge_dir / name).mkdir(parents=True, exist_ok=True)
        job_path = bridge_dir / "inbox" / "job_validator.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

        def fs_exists(path):
            candidate = Path(str(path))
            if candidate.is_dir():
                return "directory"
            if candidate.is_file():
                return "file"
            return None

        def fs_files(directory):
            directory_path = Path(str(directory))
            if not directory_path.is_dir():
                return []
            return sorted(
                entry.as_posix() for entry in directory_path.iterdir() if entry.is_file()
            )

        def fs_file_at(directory, index):
            entries = fs_files(directory)
            position = int(index)
            if 1 <= position <= len(entries):
                return entries[position - 1]
            return None

        def fs_move(source, destination):
            try:
                shutil.move(str(source), str(destination))
            except OSError:
                return False
            return True

        def fs_mkdirs(path):
            Path(str(path)).mkdir(parents=True, exist_ok=True)

        def fs_delete(path):
            candidate = Path(str(path))
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
            elif candidate.exists():
                candidate.unlink(missing_ok=True)

        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.globals().fs_exists = fs_exists
        lua.globals().fs_files = fs_files
        lua.globals().fs_file_at = fs_file_at
        lua.globals().fs_move = fs_move
        lua.globals().fs_mkdirs = fs_mkdirs
        lua.globals().fs_delete = fs_delete
        script = r"""
local unpack = table.unpack
local applied = nil
local function noop() end
local function factory(fn)
    return setmetatable({}, { __call = function(_, ...) return fn(...) end })
end
local logger = { info = noop, error = noop, warn = noop, enable = noop }
local photo = {
    getRawMetadata = function(_, key)
        if key == "fileName" then return "validator.jpg" end
        if key == "path" then return "C:/photos/validator.jpg" end
        return false
    end,
    applyDevelopSettings = function(_, settings) applied = settings end,
    createDevelopSnapshot = noop,
    getDevelopSnapshots = function() return {} end,
    deleteDevelopSnapshot = noop,
}
local catalog = {
    getTargetPhoto = function() return photo end,
    setSelectedPhotos = noop,
    createVirtualCopies = function() return { photo } end,
    withWriteAccessDo = function(_, _, fn) fn() end,
}
local modules = {
    LrApplication = { activeCatalog = function() return catalog end },
    LrApplicationView = { switchToModule = noop, showView = noop },
    LrBinding = { makePropertyTable = function() return {} end },
    LrDialogs = {
        message = noop,
        showBezel = noop,
        presentModalDialog = function() return "cancel" end,
        presentFloatingDialog = function() return nil end,
        attachErrorDialogToFunctionContext = noop,
    },
    LrFileUtils = {
        exists = fs_exists,
        files = function(directory)
            local index = 0
            return function()
                index = index + 1
                return fs_file_at(directory, index)
            end
        end,
        readFile = function()
            error("bridge jobs must be read with read_text_file, not LrFileUtils.readFile")
        end,
        createAllDirectories = fs_mkdirs,
        move = fs_move,
        delete = fs_delete,
    },
    LrFunctionContext = {
        callWithContext = function(_, fn)
            local cleanup = nil
            local context = {
                addCleanupHandler = function(_, handler) cleanup = handler end,
            }
            local result = fn(context)
            if cleanup then cleanup() end
            return result
        end,
    },
    LrHttp = { get = noop, post = noop, postMultipart = noop, openUrlInBrowser = noop },
    LrLogger = factory(function() return logger end),
    LrPathUtils = {
        child = function(a, b) return a .. "/" .. b end,
        parent = function(a) return a:match("^(.*)/[^/]+$") end,
        leafName = function(a) return a:match("([^/]+)$") end,
        getStandardFilePath = function(name)
            if name == "appPrefs" then return [[__LIGHTROOM_ROOT__]] end
            return "C:/nonexistent"
        end,
    },
    LrTasks = {
        sleep = noop,
        startAsyncTask = noop,
        pcall = function(fn) return pcall(fn) end,
    },
    LrView = {
        osFactory = function()
            return {
                column = function(_, t) return t end,
                row = function(_, t) return t end,
                static_text = function(_, t) return t end,
                popup_menu = function(_, t) return t end,
                slider = function(_, t) return t end,
                separator = function(_, t) return t or {} end,
                scrolled_view = function(_, t) return t or {} end,
                push_button = function(_, t) return t or {} end,
                checkbox = function(_, t) return t or {} end,
                progressbar = function(_, t) return t or {} end,
            }
        end,
        bind = function() return {} end,
    },
}
function import(name) return modules[name] or error("unexpected import " .. tostring(name)) end
_PLUGIN = { path = [[__PLUGIN_DIR__]] }
local Actions = dofile([[__ACTIONS_PATH__]])
Actions.poll_inbox()
assert(applied ~= nil, "bridge did not apply settings")
assert(applied.ProcessVersion == "15.4", "wrong process version")
assert(#applied.ToneCurvePV2012 == 8, "wrong tone curve size")
assert(applied.Exposure2012 == 0.2, "wrong exposure")
return "bridge-poll-ok"
""".replace("__LIGHTROOM_ROOT__", str(lightroom_root).replace("\\", "/")).replace(
            "__PLUGIN_DIR__", plugin_path
        ).replace(
            "__ACTIONS_PATH__", f"{plugin_path}/Actions.lua"
        )
        result = lua.execute(script)
    except Exception as exc:  # pragma: no cover - diagnostic branch
        return {"checked": True, "ok": False, "error": str(exc)}
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return {"checked": True, "ok": result == "bridge-poll-ok", "output": result}


def validate_json_module() -> dict:
    try:
        from lupa import LuaRuntime  # type: ignore
    except ImportError:
        return {"checked": False, "message": "lupa is not installed; Json.lua runtime check skipped."}

    lua = LuaRuntime(unpack_returned_tuples=True)
    script = (
        "local Json = dofile([[%s]])\n"
        "local value = Json.decode('{\"a\":[1,2,3],\"b\":\"中文\",\"c\":true}')\n"
        "assert(value.a[2] == 2 and value.b == '中文' and value.c == true)\n"
        "local encoded = Json.encode(value)\n"
        "local roundtrip = Json.decode(encoded)\n"
        "assert(roundtrip.a[3] == 3)\n"
        "return 'json-ok'\n"
    ) % str(PLUGIN_DIR / "Json.lua").replace("\\", "/")
    output = lua.execute(script)
    return {"checked": True, "ok": output == "json-ok", "output": output}


def validate_review_recall() -> dict:
    """Exercise the 1.7.0 recall protocol with mocked Lightroom UI modules.

    The real failure this guards against is a menu click that silently does
    nothing: the loop must re-present the window and acknowledge the request,
    a stale in-memory session must be released instead of blocking every later
    run(), and the busy flag must keep a slow preview from being killed.
    """
    try:
        from lupa import LuaRuntime  # type: ignore
    except ImportError:
        return {
            "checked": False,
            "message": "lupa is not installed; review recall check skipped.",
        }

    plugin_dir = str(PLUGIN_DIR).replace("\\", "/")
    script = r"""
unpack = table.unpack

local plugin_dir = [[__PLUGIN_DIR__]]
local present_count = 0
local close_count = 0
local sleep_count = 0
local sleep_mode = "recall"
local last_args = nil
local recall_token = nil
local stale_close_count = 0

local function noop() end

local factory_meta = {
    __index = function(_, _)
        return function(_, props)
            return props or {}
        end
    end,
}

local function make_factory()
    return setmetatable({}, factory_meta)
end

local function make_handle(args)
    return {
        toFront = function() end,
        close = function()
            close_count = close_count + 1
            -- Lightroom fires windowWillClose synchronously when a floating
            -- dialog is closed programmatically; the re-present path depends
            -- on it to reset state.suppress_close.
            if args and args.windowWillClose then
                args.windowWillClose()
            end
        end,
    }
end

local ReviewWindow

local modules = {
    LrBinding = {
        makePropertyTable = function()
            return {}
        end,
    },
    LrDialogs = {
        presentFloatingDialog = function(_, args)
            present_count = present_count + 1
            last_args = args
            args.onShow(make_handle(args))
        end,
    },
    LrFunctionContext = {
        callWithContext = function(_, fn)
            local cleanup = nil
            local context = {
                addCleanupHandler = function(_, handler)
                    cleanup = handler
                end,
            }
            local result = fn(context)
            if cleanup then
                cleanup()
            end
            return result
        end,
    },
    LrTasks = {
        sleep = function()
            sleep_count = sleep_count + 1
            assert(sleep_count <= 50, "review poll loop did not stop after 50 sleeps")
            if sleep_mode == "recall" then
                if sleep_count == 1 then
                    local ok, seq = ReviewWindow.request_recall()
                    assert(ok == true, "request_recall should accept a live loop")
                    recall_token = seq
                elseif sleep_count == 2 then
                    assert(
                        ReviewWindow.recall_acknowledged(recall_token) == true,
                        "the loop must acknowledge the recall"
                    )
                elseif sleep_count == 3 then
                    last_args.windowWillClose()
                end
            elseif sleep_mode == "close" and sleep_count == 1 then
                last_args.windowWillClose()
            end
        end,
    },
    LrView = {
        osFactory = function()
            return make_factory()
        end,
        bind = function()
            return {}
        end,
    },
}

function import(name)
    return modules[name] or error("unexpected import " .. tostring(name))
end

_PLUGIN = { path = plugin_dir }
ReviewWindow = dofile(plugin_dir .. "/ReviewWindow.lua")
ReviewWindow._log = noop

local options = {
    analysis_text = "总体判断：召回测试",
    styles = {
        { id = "warm", name = "暖调", description = "暖" },
        { id = "cool", name = "冷调", description = "冷" },
    },
    fetch_development = function(style_id)
        return { style_name = style_id, develop_settings = {} }
    end,
    apply_preview = function()
        return true
    end,
    parameter_summary = function()
        return ""
    end,
    log = noop,
}

local function reset_counters()
    present_count = 0
    close_count = 0
    sleep_count = 0
    last_args = nil
    recall_token = nil
end

-- 1. Live loop: the menu recall arrives between polls, the loop closes the old
--    window, presents a second one and acknowledges the sequence number.
reset_counters()
sleep_mode = "recall"
local decision, closed_reason = ReviewWindow.run(options)
assert(decision == nil and closed_reason == "closed", "run should report a user-closed window")
assert(present_count == 2, "recall must present a second window, got " .. tostring(present_count))
assert(close_count == 1, "the re-present must close the old window, got " .. tostring(close_count))
assert(ReviewWindow.is_active() == false, "cleanup must clear _active")

-- 2. Stale in-memory session: run() closes the orphan and starts fresh instead
--    of returning "已经有一个分析窗口打开了".
reset_counters()
ReviewWindow._active = true
ReviewWindow._pulse = 0
ReviewWindow._busy = false
ReviewWindow._dialog = {
    close = function()
        close_count = close_count + 1
    end,
}
sleep_mode = "close"
local stale_decision, stale_reason = ReviewWindow.run(options)
assert(stale_decision == nil and stale_reason == "closed", "stale run should report a user-closed window")
assert(ReviewWindow.is_active() == false, "stale run cleanup must clear _active")
assert(present_count == 1, "stale run should present exactly one window")
assert(close_count == 1, "stale handle must be closed before presenting, got " .. tostring(close_count))

-- 3. release_stale() closes the orphan and clears every flag.
reset_counters()
ReviewWindow._active = true
ReviewWindow._pulse = os.time()
ReviewWindow._dialog = {
    close = function()
        stale_close_count = stale_close_count + 1
    end,
}
ReviewWindow.release_stale()
assert(ReviewWindow._active == false, "release_stale must clear _active")
assert(ReviewWindow._dialog == nil, "release_stale must clear the handle")
assert(stale_close_count == 1, "release_stale must close the orphan window")

-- 4. An inactive module only reports "inactive"; nothing to hang on.
ReviewWindow._active = false
local recalled, reason = ReviewWindow.request_recall()
assert(recalled == false and reason == "inactive", "inactive request_recall must say inactive")

-- 5. Busy loop: a preview request that is still running counts as alive; the
--    busy flag expires so a truly wedged loop can still be rebuilt.
ReviewWindow._active = true
ReviewWindow._busy = true
ReviewWindow._busy_since = os.time()
assert(ReviewWindow.is_busy_alive() == true, "recent busy flag must count as alive")
assert(ReviewWindow.is_live() == true, "busy loop must be live")
ReviewWindow._busy_since = os.time() - (ReviewWindow.BUSY_LIMIT_SECONDS + 5)
assert(ReviewWindow.is_busy_alive() == false, "expired busy flag must not count as alive")
assert(ReviewWindow.is_live() == false, "expired busy loop must not be live")
ReviewWindow._active = false
ReviewWindow._busy = false

return "review-recall-ok"
""".replace("__PLUGIN_DIR__", plugin_dir)
    try:
        output = LuaRuntime(unpack_returned_tuples=True).execute(script)
    except Exception as exc:  # pragma: no cover - diagnostic branch
        return {"checked": True, "ok": False, "error": str(exc)}
    return {"checked": True, "ok": output == "review-recall-ok", "output": output}


def validate_watcher_guard() -> dict:
    """Prove the async watcher survives one throwing inbox poll.

    The real failure this guards against: a single Lua error inside
    Actions.poll_inbox (a failed catalog write, an unreadable job directory)
    used to terminate the whole async watcher task, silently disabling
    auto-apply until Lightroom restarted. InitPlugin must wrap the poll in
    LrTasks.pcall, log the failure and keep looping.
    """
    try:
        from lupa import LuaRuntime  # type: ignore
    except ImportError:
        return {
            "checked": False,
            "message": "lupa is not installed; watcher guard check skipped.",
        }

    plugin_path = str(PLUGIN_DIR).replace("\\", "/")
    script = r"""
local unpack = table.unpack
local sleep_count = 0
local info_log = {}
local watcher_ok = true
local watcher_error = nil
local cleanup_ran = false
local function noop() end
local function factory(fn)
    return setmetatable({}, { __call = function(_, ...) return fn(...) end })
end
local logger = {
    info = function(_, message) info_log[#info_log + 1] = tostring(message) end,
    error = noop,
    warn = noop,
    enable = noop,
}
local photo = {
    getRawMetadata = function(_, key) return false end,
    applyDevelopSettings = noop,
    createDevelopSnapshot = noop,
}
local catalog = {
    getTargetPhoto = function() return photo end,
    setSelectedPhotos = noop,
    createVirtualCopies = function() return { photo } end,
    withWriteAccessDo = function(_, _, fn) fn() end,
}
local modules = {
    LrApplication = { activeCatalog = function() return catalog end },
    LrApplicationView = { switchToModule = noop, showView = noop },
    LrBinding = { makePropertyTable = function() return {} end },
    LrDialogs = {
        message = noop,
        showBezel = noop,
        presentModalDialog = function() return "cancel" end,
        presentFloatingDialog = function() return nil end,
        attachErrorDialogToFunctionContext = noop,
    },
    LrFileUtils = {
        exists = function(path)
            if tostring(path):match("AI Photo Agent Bridge") then return "directory" end
            return nil
        end,
        files = function() error("mock inbox enumeration failure") end,
        readFile = function()
            error("bridge jobs must be read with read_text_file, not LrFileUtils.readFile")
        end,
        createAllDirectories = noop,
        move = function() return true end,
        delete = noop,
    },
    LrFunctionContext = {
        postAsyncTaskWithContext = function(name, fn)
            local cleanup = nil
            local context = {
                addCleanupHandler = function(_, handler) cleanup = handler end,
            }
            watcher_ok, watcher_error = pcall(fn, context)
            if cleanup then cleanup() end
            cleanup_ran = true
        end,
        callWithContext = function(_, fn)
            local cleanup = nil
            local context = {
                addCleanupHandler = function(_, handler) cleanup = handler end,
            }
            local result = fn(context)
            if cleanup then cleanup() end
            return result
        end,
    },
    LrHttp = { get = noop, post = noop, postMultipart = noop, openUrlInBrowser = noop },
    LrLogger = factory(function() return logger end),
    LrPathUtils = {
        child = function(a, b) return a .. "/" .. b end,
        parent = function(a) return a:match("^(.*)/[^/]+$") end,
        leafName = function(a) return a:match("([^/]+)$") end,
        getStandardFilePath = function() return "C:/LR" end,
    },
    LrTasks = {
        sleep = function()
            sleep_count = sleep_count + 1
            if sleep_count == 1 then
                error("watcher-stop-sentinel")
            end
            assert(sleep_count <= 50, "watcher loop did not stop after 50 sleeps")
        end,
        startAsyncTask = noop,
        pcall = function(fn) return pcall(fn) end,
    },
    LrView = {
        osFactory = function()
            return {
                column = function(_, t) return t end,
                row = function(_, t) return t end,
                static_text = function(_, t) return t end,
                popup_menu = function(_, t) return t end,
                slider = function(_, t) return t end,
                separator = function(_, t) return t or {} end,
                scrolled_view = function(_, t) return t or {} end,
                push_button = function(_, t) return t or {} end,
                checkbox = function(_, t) return t or {} end,
                progressbar = function(_, t) return t or {} end,
            }
        end,
        bind = function() return {} end,
    },
}
function import(name) return modules[name] or error("unexpected import " .. tostring(name)) end
_PLUGIN = { path = [[__PLUGIN_DIR__]] }
dofile([[__PLUGIN_DIR__/Actions.lua]])
dofile([[__PLUGIN_DIR__/InitPlugin.lua]])
assert(
    watcher_ok == false,
    "the watcher must stop at the sentinel sleep, got ok=" .. tostring(watcher_ok)
)
assert(
    tostring(watcher_error):find("watcher-stop-sentinel", 1, true) ~= nil,
    "the sentinel error must propagate, got " .. tostring(watcher_error)
)
local joined = table.concat(info_log, "\n")
assert(
    joined:find("watcher: poll failed:", 1, true) ~= nil,
    "a failing poll must be logged, got: " .. joined
)
assert(_G._AIPhotoAgentWatcherStarted == false, "the cleanup handler must reset the watcher flag")
assert(cleanup_ran == true, "postAsyncTaskWithContext mock must run the cleanup handler")
return "watcher-guard-ok"
""".replace("__PLUGIN_DIR__", plugin_path)
    try:
        output = LuaRuntime(unpack_returned_tuples=True).execute(script)
    except Exception as exc:  # pragma: no cover - diagnostic branch
        return {"checked": True, "ok": False, "error": str(exc)}
    return {"checked": True, "ok": output == "watcher-guard-ok", "output": output}


def validate_topmost_helper() -> dict:
    """Parse and smoke-run the Windows topmost helper.

    The helper emulates Lightroom 15.2's forced-topmost floating dialogs on
    15.1. It must parse, compile its P/Invoke class, and safely report "no
    window" when given a title prefix that cannot exist on this machine.
    """
    helper = PLUGIN_DIR / "ReviewWindowTopmost.ps1"
    if not helper.is_file():
        return {"checked": True, "ok": False, "error": "ReviewWindowTopmost.ps1 is missing"}

    if os.name != "nt":
        return {
            "checked": False,
            "message": "not Windows; topmost helper check skipped.",
        }
    powershell = shutil.which("powershell")
    if not powershell:
        return {
            "checked": False,
            "message": "powershell not found; topmost helper check skipped.",
        }

    path = str(helper)
    parse_script = (
        "$tokens = $null; $errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', "
        "[ref]$tokens, [ref]$errors); "
        "if ($errors -and $errors.Count -gt 0) { "
        "$errors | ForEach-Object { Write-Output $_.Message }; exit 1 }"
    )
    try:
        parsed = subprocess.run(
            [powershell, "-NoProfile", "-Command", parse_script],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:  # pragma: no cover - diagnostic branch
        return {"checked": True, "ok": False, "error": f"parse failed: {exc}"}
    if parsed.returncode != 0:
        return {
            "checked": True,
            "ok": False,
            "error": f"parse failed: {(parsed.stdout or parsed.stderr).strip()}",
        }

    try:
        smoke = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                path,
                "-Once",
                "-TitlePrefix",
                "AIPA_VALIDATOR_NO_SUCH_WINDOW",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # pragma: no cover - diagnostic branch
        return {"checked": True, "ok": False, "error": f"smoke run failed: {exc}"}
    # 2 = helper ran correctly and found nothing, which is exactly what a
    # nonexistent prefix must produce. Any other code means a broken helper.
    return {
        "checked": True,
        "ok": smoke.returncode == 2,
        "parse": "ok",
        "smoke_exit": smoke.returncode,
        "stderr": (smoke.stderr or "").strip()[:500],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=DEFAULT_TOOLS_DIR,
        help="Optional directory containing luaparser/lupa (data/runtime/tools by default).",
    )
    args = parser.parse_args()

    luaparser_module = _load_luaparser(args.tools_dir.resolve())
    report = {
        "package": validate_package(),
        "syntax": validate_syntax(luaparser_module),
        "json_runtime": validate_json_module(),
        "bridge_runtime": validate_bridge_runtime(),
        "review_recall": validate_review_recall(),
        "watcher_guard": validate_watcher_guard(),
        "topmost_helper": validate_topmost_helper(),
    }
    checks = [report["package"]]
    if report["syntax"].get("checked"):
        checks.append(report["syntax"])
    if report["json_runtime"].get("checked"):
        checks.append(report["json_runtime"])
    if report["bridge_runtime"].get("checked"):
        checks.append(report["bridge_runtime"])
    if report["review_recall"].get("checked"):
        checks.append(report["review_recall"])
    if report["watcher_guard"].get("checked"):
        checks.append(report["watcher_guard"])
    if report["topmost_helper"].get("checked"):
        checks.append(report["topmost_helper"])

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(check.get("ok") for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
