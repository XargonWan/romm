import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import handler.install.runner as runner


class TestUsesWine:
    def test_true_for_a_windows_installer(self):
        assert runner._uses_wine("/library/win/game/setup.exe") is True

    @pytest.mark.parametrize("ext", [".sh", ".run", ".SH", ".RUN"])
    def test_false_for_native_linux_installers(self, ext):
        assert runner._uses_wine(f"/library/linux/game/install{ext}") is False


class TestWineOrProton:
    def test_defaults_to_wine_when_no_builds_installed(self, monkeypatch):
        monkeypatch.setattr(runner, "resolve_proton_path", lambda _: None)
        monkeypatch.setattr(runner, "list_proton_builds", lambda: [])
        assert runner._wine_or_proton() == "wine"

    def test_uses_resolved_proton_path(self, monkeypatch):
        monkeypatch.setattr(
            runner, "resolve_proton_path", lambda _: "/opt/proton/GE-Proton10-34/proton"
        )
        assert runner._wine_or_proton("GE-Proton10-34") == "/opt/proton/GE-Proton10-34/proton"

    def test_unset_proton_build_falls_back_to_first_installed(self, monkeypatch):
        from handler.install.proton_builds import ProtonBuild

        monkeypatch.setattr(runner, "resolve_proton_path", lambda _: None)
        monkeypatch.setattr(
            runner,
            "list_proton_builds",
            lambda: [
                ProtonBuild(
                    id="GE-Proton10-34",
                    label="GE-Proton 10-34",
                    installed=True,
                    path="/opt/proton/GE-Proton10-34/proton",
                ),
                ProtonBuild(
                    id="cachyos-latest",
                    label="Proton-CachyOS",
                    installed=True,
                    path="/opt/proton/cachyos-latest/proton",
                ),
            ],
        )
        # With proton_build=None, falls back to the first installed build.
        assert runner._wine_or_proton(None) == "/opt/proton/GE-Proton10-34/proton"

    def test_unknown_proton_build_falls_back_to_first_installed(
        self, monkeypatch
    ):
        from handler.install.proton_builds import ProtonBuild

        monkeypatch.setattr(runner, "resolve_proton_path", lambda _: None)
        monkeypatch.setattr(
            runner,
            "list_proton_builds",
            lambda: [
                ProtonBuild(
                    id="GE-Proton10-34",
                    label="GE-Proton 10-34",
                    installed=True,
                    path="/opt/proton/GE-Proton10-34/proton",
                ),
            ],
        )
        assert runner._wine_or_proton("not-a-real-build") == "/opt/proton/GE-Proton10-34/proton"

    def test_recognized_proton_build_resolves_via_the_registry(self, monkeypatch):
        monkeypatch.setattr(
            runner, "resolve_proton_path", lambda build_id: "/opt/proton/other/proton"
        )
        assert runner._wine_or_proton("some-build") == "/opt/proton/other/proton"


class TestIsProton:
    def test_true_for_a_path_named_proton(self):
        assert runner._is_proton("/opt/proton/proton") is True

    def test_false_for_plain_wine(self):
        assert runner._is_proton("wine") is False

    def test_false_for_a_differently_named_wine_build(self):
        assert runner._is_proton("/opt/lutris/wine-ge/bin/wine") is False


class TestWineDriveCRoot:
    def test_proton_nests_under_pfx(self):
        assert runner._wine_drive_c_root(
            Path("/cache/1/prefix"), "/opt/proton/proton"
        ) == Path("/cache/1/prefix/pfx")

    def test_plain_wine_uses_the_prefix_directly(self):
        assert runner._wine_drive_c_root(Path("/cache/1/prefix"), "wine") == Path(
            "/cache/1/prefix"
        )


class TestBuildInnerCommand:
    def test_wraps_windows_installer_with_wine(self):
        assert runner._build_inner_command("/library/win/game/setup.exe", "wine") == [
            "wine",
            "/library/win/game/setup.exe",
        ]

    def test_wraps_windows_installer_with_proton_waitforexitandrun(self):
        # Not "run": protonfixes.execute() only applies Proton's own
        # per-game/per-installer compatibility fixes for this verb.
        assert runner._build_inner_command(
            "/library/win/game/setup.exe", "/opt/proton/proton"
        ) == ["/opt/proton/proton", "waitforexitandrun", "/library/win/game/setup.exe"]

    def test_runs_native_installers_directly_through_sh_even_with_proton(self):
        # .sh/.run installers never touch Wine/Proton regardless of what's
        # configured - _uses_wine gates this before proton_or_wine matters.
        assert runner._build_inner_command(
            "/library/linux/game/install.sh", "/opt/proton/proton"
        ) == ["/bin/sh", "/library/linux/game/install.sh"]


class TestWrapForSandbox:
    """bwrap only exposes what's explicitly bound in. Now only the resolved
    Proton build's own directory is bound (not all env-var paths), and only
    if it actually exists — guarding against the bwrap crash when a stale
    env var points at a non-existent path."""

    def _captured_ro_binds(self, monkeypatch) -> list[tuple[str, ...]]:
        captured: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            runner,
            "build_bwrap_command",
            lambda spec, inner: captured.append(spec.ro_binds) or inner,
        )
        return captured

    def test_binds_only_the_resolved_proton_builds_directory(self, monkeypatch):
        captured = self._captured_ro_binds(monkeypatch)

        runner._wrap_for_sandbox(
            ["true"],
            installer_abs="/library/win/game/setup.exe",
            work_dir="/work",
            proton_prefix="/prefix",
            display=":50",
            proton_or_wine="/opt/proton/GE-Proton10-34/proton",
        )

        # Only the selected build's directory is bound, not all possible ones.
        assert captured[0] == ("/opt/proton/GE-Proton10-34",)

    def test_binds_nothing_for_plain_wine(self, monkeypatch):
        captured = self._captured_ro_binds(monkeypatch)

        runner._wrap_for_sandbox(
            ["true"],
            installer_abs="/library/win/game/setup.exe",
            work_dir="/work",
            proton_prefix="/prefix",
            display=":50",
            proton_or_wine="wine",
        )

        # Plain Wine lives under /usr which is already ro-bound by the
        # sandbox spec; no extra ro_bind is needed.
        assert captured[0] == ()

    def test_binds_nothing_when_proton_path_doesnt_exist(self, monkeypatch):
        # The crash fix: a stale/non-existent path must not attempt a bwrap
        # bind (which fails with "Can't find source path"), just skip it.
        captured = self._captured_ro_binds(monkeypatch)

        runner._wrap_for_sandbox(
            ["true"],
            installer_abs="/library/win/game/setup.exe",
            work_dir="/work",
            proton_prefix="/prefix",
            display=":50",
            proton_or_wine="/opt/proton/GE-Proton10-34/proton",
        )
        # Path doesn't exist on the test machine, so no ro_bind is set.
        assert captured[0] == ()

    def test_sandbox_disabled_skips_bwrap_entirely(self, monkeypatch):
        monkeypatch.setattr(runner, "INSTALL_SANDBOX_ENABLED", False)
        result = runner._wrap_for_sandbox(
            ["true"],
            installer_abs="/library/win/game/setup.exe",
            work_dir="/work",
            proton_prefix="/prefix",
            display=":50",
            proton_or_wine="wine",
        )
        assert result == ["true"]


class TestInitWinePrefix:
    def test_runs_wineboot_init_wrapped_in_the_sandbox(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            runner, "_wrap_for_sandbox", lambda inner, **kw: ["bwrap", "--", *inner]
        )
        monkeypatch.setattr(
            runner.subprocess,
            "run",
            lambda argv, **kw: calls.append((argv, kw)),
        )

        runner._init_wine_prefix(
            "wine",
            installer_abs="/library/win/game/setup.exe",
            work_dir="/cache/1",
            prefix_dir="/cache/1/prefix",
            display=":99",
        )

        argv, kwargs = calls[0]
        assert argv == ["bwrap", "--", "wine", "wineboot", "--init"]
        assert kwargs["timeout"] == runner.WINE_PREFIX_INIT_TIMEOUT
        assert kwargs["check"] is True

    def test_runs_wineboot_init_through_proton_run(self, monkeypatch):
        wrapped = []
        monkeypatch.setattr(
            runner,
            "_wrap_for_sandbox",
            lambda inner, **kw: (wrapped.append((inner, kw)), ["bwrap", "--", *inner])[
                1
            ],
        )
        monkeypatch.setattr(runner.subprocess, "run", lambda argv, **kw: None)

        runner._init_wine_prefix(
            "/opt/proton/proton",
            installer_abs="/library/win/game/setup.exe",
            work_dir="/cache/1",
            prefix_dir="/cache/1/prefix",
            display=":99",
            extra_env=(("STEAM_COMPAT_DATA_PATH", "/cache/1/prefix"),),
        )

        inner, kwargs = wrapped[0]
        assert inner == ["/opt/proton/proton", "run", "wineboot", "--init"]
        assert kwargs["extra_env"] == (("STEAM_COMPAT_DATA_PATH", "/cache/1/prefix"),)

    def test_retries_once_and_succeeds_on_a_transient_failure(self, monkeypatch):
        monkeypatch.setattr(runner, "_wrap_for_sandbox", lambda inner, **kw: inner)
        sleeps = []
        monkeypatch.setattr(runner.time, "sleep", sleeps.append)

        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(
                    returncode=53, cmd=argv, stderr=b"transient X race"
                )

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        runner._init_wine_prefix(
            "wine",
            installer_abs="/library/win/game/setup.exe",
            work_dir="/cache/1",
            prefix_dir="/cache/1/prefix",
            display=":99",
        )  # must not raise

        assert len(calls) == 2
        assert sleeps == [runner.WINE_PREFIX_INIT_RETRY_DELAY]

    def test_raises_a_clear_error_including_stderr_after_all_attempts_fail(
        self, monkeypatch
    ):
        monkeypatch.setattr(runner, "_wrap_for_sandbox", lambda inner, **kw: inner)
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)

        def fake_run(argv, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=53, cmd=argv, stderr=b"kernel32.dll not found"
            )

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        with pytest.raises(
            RuntimeError, match="Failed to initialize the Wine prefix.*kernel32"
        ):
            runner._init_wine_prefix(
                "wine",
                installer_abs="/library/win/game/setup.exe",
                work_dir="/cache/1",
                prefix_dir="/cache/1/prefix",
                display=":99",
            )

    def test_error_label_says_proton_when_using_proton(self, monkeypatch):
        monkeypatch.setattr(runner, "_wrap_for_sandbox", lambda inner, **kw: inner)
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)

        def fake_run(argv, **kwargs):
            raise subprocess.CalledProcessError(returncode=1, cmd=argv, stderr=b"")

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        with pytest.raises(
            RuntimeError, match="Failed to initialize the Proton prefix"
        ):
            runner._init_wine_prefix(
                "/opt/proton/proton",
                installer_abs="/library/win/game/setup.exe",
                work_dir="/cache/1",
                prefix_dir="/cache/1/prefix",
                display=":99",
            )

    def test_raises_a_clear_error_on_timeout(self, monkeypatch):
        monkeypatch.setattr(runner, "_wrap_for_sandbox", lambda inner, **kw: inner)
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        with pytest.raises(
            RuntimeError, match="Timed out initializing the Wine prefix"
        ):
            runner._init_wine_prefix(
                "wine",
                installer_abs="/library/win/game/setup.exe",
                work_dir="/cache/1",
                prefix_dir="/cache/1/prefix",
                display=":99",
            )


class TestFocusMaintenanceLoop:
    def test_focuses_immediately_then_on_every_interval(self, monkeypatch):
        monkeypatch.setattr(runner, "FOCUS_MAINTENANCE_INTERVAL", 0)

        focus_calls: list[dict] = []
        stop = threading.Event()

        def fake_focus(env):
            focus_calls.append(env)
            if len(focus_calls) >= 3:
                stop.set()

        monkeypatch.setattr(runner, "_focus_installer_window", fake_focus)

        runner._focus_maintenance_loop(":99", stop)

        assert len(focus_calls) == 3
        assert all(env["DISPLAY"] == ":99" for env in focus_calls)

    def test_never_sends_any_key(self, monkeypatch):
        # This loop only ever grabs focus - it must never type or click on
        # the user's behalf; they drive the installer through VNC themselves.
        monkeypatch.setattr(runner, "FOCUS_MAINTENANCE_INTERVAL", 0)
        monkeypatch.setattr(runner, "_focus_installer_window", lambda env: None)

        key_calls = []
        monkeypatch.setattr(
            runner.subprocess,
            "run",
            lambda argv, **kw: key_calls.append(argv),
        )

        stop = threading.Event()
        stop.set()
        runner._focus_maintenance_loop(":99", stop)

        assert key_calls == []

    def test_stops_promptly_when_signalled(self, monkeypatch):
        focus_calls: list[dict] = []
        monkeypatch.setattr(
            runner, "_focus_installer_window", lambda env: focus_calls.append(env)
        )

        stop = threading.Event()
        stop.set()
        runner._focus_maintenance_loop(":99", stop)

        # The immediate focus call still happens once before the loop checks
        # `stop`, but nothing after.
        assert len(focus_calls) == 1


class TestFocusInstallerWindow:
    def _fake_run(self, window_names: dict[str, tuple[str, str]]):
        """window_names: {window_id: (name, "WIDTHxHEIGHT")}."""
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            out = MagicMock()
            if argv[1] == "search":
                out.stdout = " ".join(window_names.keys())
            elif argv[1] == "getwindowname":
                out.stdout = window_names[argv[-1]][0]
            elif argv[1] == "getwindowgeometry":
                w, h = window_names[argv[-1]][1].split("x")
                out.stdout = f"WIDTH={w}\nHEIGHT={h}\n"
            else:
                out.stdout = ""
            return out

        return fake_run, calls

    def test_focuses_the_largest_non_chrome_window(self, monkeypatch):
        fake_run, calls = self._fake_run(
            {
                "1": ("IceTopWin", "1x1"),
                "2": ("Select Setup Language", "297x125"),
                "3": ("Default IME", "1x1"),
                # IceWM's own per-window decoration wrapper - same name for
                # every managed window, including the real one above. Its
                # geometry can tie or beat the client's; must still lose.
                "4": ("Frame", "301x148"),
            }
        )
        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        runner._focus_installer_window({"DISPLAY": ":99"})

        focus_call = next(c for c in calls if c[1] == "windowfocus")
        assert focus_call == ["xdotool", "windowfocus", "--sync", "2"]

    def test_does_nothing_when_only_chrome_windows_exist(self, monkeypatch):
        fake_run, calls = self._fake_run(
            {"1": ("IceTopWin", "1x1"), "2": ("IceBottom", "1x1")}
        )
        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        runner._focus_installer_window({"DISPLAY": ":99"})

        assert not any(c[1] == "windowfocus" for c in calls)

    def test_swallows_missing_binary(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise FileNotFoundError("xdotool not installed")

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        # Must not raise: a missing/misbehaving xdotool shouldn't crash the install.
        runner._focus_installer_window({"DISPLAY": ":99"})


class TestRunInstaller:
    def test_waits_for_process_and_stops_focus_loop(self, monkeypatch):
        events: list[str] = []

        class FakeProc:
            def wait(self, timeout=None):
                events.append("wait")
                return 0

        monkeypatch.setattr(
            runner.subprocess,
            "Popen",
            lambda argv: (events.append(("popen", argv)), FakeProc())[1],
        )

        stopped_with: list[str] = []

        def fake_loop(display, stop):
            stop.wait(2)
            stopped_with.append(display)

        monkeypatch.setattr(runner, "_focus_maintenance_loop", fake_loop)

        runner._run_installer(["true"], ":50")

        assert events[0] == ("popen", ["true"])
        assert "wait" in events
        assert stopped_with == [":50"]

    def test_kills_process_and_reraises_on_timeout(self, monkeypatch):
        class FakeProc:
            def __init__(self):
                self.wait_calls = 0
                self.killed = False

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired(cmd="installer", timeout=timeout)
                return 0

            def kill(self):
                self.killed = True

        fake = FakeProc()
        monkeypatch.setattr(runner.subprocess, "Popen", lambda argv: fake)
        monkeypatch.setattr(
            runner, "_focus_maintenance_loop", lambda display, stop: stop.wait(2)
        )

        with pytest.raises(subprocess.TimeoutExpired):
            runner._run_installer(["true"], ":50")

        assert fake.killed is True
        assert fake.wait_calls == 2
