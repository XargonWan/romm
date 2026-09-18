from handler.install import proton_builds


class TestListProtonBuilds:
    def test_returns_the_known_builds(self):
        builds = proton_builds.list_proton_builds()
        assert len(builds) > 0
        assert any(b.installed for b in builds)

    def test_exactly_two_builds_are_installed(self):
        # GE-Proton and Proton-CachyOS are both actually baked into the
        # sandbox image, side by side - everything else here is scaffolding
        # for later.
        installed = [b for b in proton_builds.list_proton_builds() if b.installed]
        assert len(installed) == 2


class TestResolveProtonPath:
    def test_none_returns_none(self):
        assert proton_builds.resolve_proton_path(None) is None

    def test_unknown_build_id_returns_none(self):
        assert proton_builds.resolve_proton_path("not-a-real-build") is None

    def test_known_but_not_installed_build_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            proton_builds,
            "KNOWN_PROTON_BUILDS",
            (
                proton_builds.ProtonBuild(
                    id="future-build", label="Future", installed=False
                ),
            ),
        )
        assert proton_builds.resolve_proton_path("future-build") is None

    def test_installed_build_resolves_to_its_own_path(self, monkeypatch):
        monkeypatch.setattr(
            proton_builds,
            "KNOWN_PROTON_BUILDS",
            (proton_builds.ProtonBuild(id="current", label="Current", installed=True),),
        )
        monkeypatch.setattr(
            proton_builds, "_BUILD_PATHS", {"current": "/opt/proton/proton"}
        )
        assert proton_builds.resolve_proton_path("current") == "/opt/proton/proton"

    def test_each_installed_build_resolves_to_its_own_distinct_path(self, monkeypatch):
        # Two builds installed side by side must never collapse onto the
        # same binary - each id maps to its own path.
        monkeypatch.setattr(
            proton_builds,
            "KNOWN_PROTON_BUILDS",
            (
                proton_builds.ProtonBuild(id="ge", label="GE", installed=True),
                proton_builds.ProtonBuild(
                    id="cachyos", label="CachyOS", installed=True
                ),
            ),
        )
        monkeypatch.setattr(
            proton_builds,
            "_BUILD_PATHS",
            {"ge": "/opt/proton-ge/proton", "cachyos": "/opt/proton-cachyos/proton"},
        )
        assert proton_builds.resolve_proton_path("ge") == "/opt/proton-ge/proton"
        assert (
            proton_builds.resolve_proton_path("cachyos") == "/opt/proton-cachyos/proton"
        )
