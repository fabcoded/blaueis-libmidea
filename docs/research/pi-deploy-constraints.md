# Raspberry Pi deployment constraints per install mechanism

> Research note, 2026-09-02. Input for the `0.1.0` release planning: what a
> stranger's Raspberry Pi imposes on each candidate install mechanism for
> `blaueis-gateway`. Facts only, each with its source; no decision is made
> here. Versions and wheel lists were read from PyPI / piwheels / Debian on
> the date above and will drift.

Mechanisms compared:

- **A** — `git clone --depth 1 --branch v<ver>` of the monorepo into
  `/opt/blaueis-gw`, venv, `pip install -e packages/blaueis-core -e
  packages/blaueis-gateway` (today's `scripts/install.sh` minus the
  `main`-tracking).
- **B** — venv, `pip install blaueis-gateway==<ver>` from PyPI.
- **C** — venv, `pip install "blaueis-gateway @
  git+https://github.com/fabcoded/blaueis-libmidea@v<ver>#subdirectory=packages/blaueis-gateway"`.

## Summary

- A venv is mandatory on every Pi OS since Bookworm (PEP 668); this and the system Python (3.11 Bookworm, 3.13 Trixie) are identical for A, B and C.
- All three run the same pip resolution over the same dependency set, so wheel availability, source-build risk and dependency install time are identical.
- 64-bit Pi OS gets manylinux aarch64 wheels from PyPI for every compiled dependency; 32-bit Pi OS relies on piwheels (preconfigured in `/etc/pip.conf`, 32-bit only) for `pyyaml` and `cffi`, while `cryptography`, `websockets` and `rpds-py` now ship armv7l wheels on PyPI too.
- Without a wheel, `cryptography` needs Rust >= 1.83; Bookworm's apt rustc is 1.63, so a source build there means rustup and a long compile.
- A wheel cannot deliver systemd units or the bash CLI to `/etc` or `/usr/local/bin`: wheel `data` lands under the venv prefix and setuptools marks `data-files` "Discouraged"; B and C need a post-install step (console script + package data) that A gets from the checkout.
- Version: `importlib.metadata.version()` works for all three; only A can run `git describe`; only C records tag + commit in `direct_url.json`.
- Offline rollback: A if the old tag's objects are local, B only with a pre-downloaded wheelhouse, C never (pip re-clones; tag builds are not cached).
- Integrity: B gets PEP 740 attestations (pip does not verify them yet); A and C get the git tag (signed and/or under an immutable GitHub release); the `curl | bash` bootstrap from a `main` URL is the same weak point for all three.

## 1. Python packaging on Raspberry Pi OS Bookworm and later

**PEP 668 / externally managed environments.** The `EXTERNALLY-MANAGED`
marker in the stdlib directory tells installers that the distro owns the
interpreter; outside a virtual environment "the installer should exit with an
error message indicating that package installation into this Python
interpreter's directory are disabled outside of a virtual environment". The
venv check is `sys.prefix == sys.base_prefix`. The override "such as a
command-line flag `--break-system-packages`" "should not be enabled by default
and should carry some connotation that its use is risky"
([PEP 668](https://peps.python.org/pep-0668/)).

**Raspberry Pi OS applies it from Bookworm on.** "From Raspberry Pi OS
Bookworm onwards, you cannot install libraries directly into the system
version of Python"; the documented route is `python -m venv <env-name>`, with
`--system-site-packages` to "preload all of the currently installed packages
in your system Python installation into the virtual environment"
([raspberrypi.com — Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html)).
The Bookworm announcement: "From Bookworm onwards packages installed via pip
must be installed into a Python Virtual Environment using venv. This has been
introduced by the Python community not Raspberry Pi, see PEP 668", Python
3.11.2 ([raspberrypi.com — Bookworm](https://www.raspberrypi.com/news/bookworm-the-new-version-of-raspberry-pi-os/)).

**System Python per release.** Debian bookworm `python3` is 3.11.2
([python3-venv bookworm](https://packages.debian.org/bookworm/python3-venv)
depends on `python3 (= 3.11.2-1+b1)`); Debian trixie `python3` is
`3.13.5-1`, depending on `python3.13`
([packages.debian.org/trixie/python3](https://packages.debian.org/trixie/python3)).
Raspberry Pi OS Trixie was announced 2025-10-02
([raspberrypi.com — Trixie](https://www.raspberrypi.com/news/trixie-the-new-version-of-raspberry-pi-os/));
piwheels lists "Bookworm / Python 3.11" and "Trixie / Python 3.13"
([piwheels.org](https://www.piwheels.org/)). Our packages declare
`requires-python = ">=3.11"`; so do `websockets` 17.1 and `rpds-py`
2026.6.3 (PyPI JSON, see section 2) — Bullseye (3.9) is out of scope for the
dependency set regardless of mechanism.

**`python3-venv` and the pip inside the venv.** Bookworm's `python3-venv`
(3.11.2-1+b1) depends on `python3.11-venv`, which depends on
`python3-pip-whl (>= 22.2)` and `python3-setuptools-whl`
([python3-venv](https://packages.debian.org/bookworm/python3-venv),
[python3.11-venv](https://packages.debian.org/bookworm/python3.11-venv)).
`python3-pip-whl` in bookworm is `23.0.1+dfsg-1`
([python3-pip-whl](https://packages.debian.org/bookworm/python3-pip-whl));
trixie's `python3-pip` is `25.1.1+dfsg-1`
([python3-pip trixie](https://packages.debian.org/trixie/python3-pip)).
`venv` bootstraps that pip: "Unless the `--without-pip` option is given,
`ensurepip` will be invoked to bootstrap `pip` into the virtual environment";
`--upgrade-deps` upgrades it to the latest on PyPI
([docs.python.org — venv](https://docs.python.org/3/library/venv.html)).
The Raspberry Pi OS Lite image build list (`stage2/01-sys-tweaks/00-packages`)
contains `python3-venv`, `curl` and `raspberrypi-sys-mods`; `git` does not
appear in that file
([pi-gen stage2 packages](https://raw.githubusercontent.com/RPi-Distro/pi-gen/master/stage2/01-sys-tweaks/00-packages)).

**Per mechanism.** Identical: the venv, the system Python, the venv's pip
version. Differs: A and C invoke `git` (pip's VCS support requires "a working
executable to be available" for the VCS,
[pip — VCS support](https://pip.pypa.io/en/stable/topics/vcs-support/));
B needs only Python.

## 2. Binary wheels for the dependency set

Runtime dependency closure (from `packages/*/pyproject.toml` plus PyPI
`requires_dist`): `cryptography` (-> `cffi` on CPython, -> `pycparser`),
`pyyaml`, `websockets`, `jsonschema` (-> `attrs`, `referencing`,
`jsonschema-specifications`, `rpds-py`), `pyserial-asyncio` (-> `pyserial`).

**PyPI wheel coverage on 2026-09-02** (latest release, file list from
`https://pypi.org/pypi/<name>/json`):

| Package | Version | aarch64 (manylinux) | armv7l (manylinux) | Pure `none-any` | Notes |
|---|---|---|---|---|---|
| cryptography | 50.0.1 | 2_17 / 2_28 / 2_34 (cp39-abi3, cp311-abi3) | **2_31** (cp39-abi3, cp311-abi3) | no | requires `cffi>=2.0.0` on CPython ([JSON](https://pypi.org/pypi/cryptography/json)) |
| cffi | 2.1.1 | 2_17 (cp310–cp315) | **none** | no | sdist only for armv7l ([JSON](https://pypi.org/pypi/cffi/json)) |
| pyyaml | 6.0.3 | 2014/2_17/2_28 (cp38–cp314) | **none** | no | sdist only for armv7l ([JSON](https://pypi.org/pypi/pyyaml/json)) |
| websockets | 17.1 | 2014/2_17/2_28 (cp311–cp315) | 2014/2_17/2_31 + musllinux | **yes** (`py3-none-any`) | ([JSON](https://pypi.org/pypi/websockets/json)) |
| rpds-py | 2026.6.3 | 2_17 (cp311–cp315) | 2_17 | no | ([JSON](https://pypi.org/pypi/rpds-py/json)) |
| jsonschema | 4.26.0 | — | — | yes | deps `attrs`, `referencing`, `jsonschema-specifications` are all `py3-none-any`; `rpds-py>=0.25.0` ([JSON](https://pypi.org/pypi/jsonschema/json)) |
| pyserial-asyncio | 0.6 | — | — | yes | `pyserial` 3.5 is `py2.py3-none-any` ([JSON](https://pypi.org/pypi/pyserial-asyncio/json), [pyserial](https://pypi.org/pypi/pyserial/json)) |
| pycparser | 3.0 | — | — | yes | ([JSON](https://pypi.org/pypi/pycparser/json)) |

**glibc gate for manylinux tags.** A `manylinux_X_Y_<arch>` wheel is
compatible when `(sys_major, sys_minor) >= (tag_major, tag_minor)`; the tag
applies to `aarch64` and `armv7l` among others
([PEP 600](https://peps.python.org/pep-0600/)). Bookworm ships `libc6
2.36-9+deb12u14`, trixie `libc6 2.41-12+deb13u3`
([bookworm](https://packages.debian.org/bookworm/libc6),
[trixie](https://packages.debian.org/trixie/libc6)) — both accept
`manylinux_2_31_armv7l` and `manylinux_2_34_aarch64`.

**64-bit Pi OS.** Every compiled dependency has an aarch64 manylinux wheel on
PyPI (table above). piwheels is not involved: "Our wheels are only supported
under 32-bit (`armhf`) Raspberry Pi OS" ([piwheels.org](https://www.piwheels.org/));
"The repository at piwheels.org does not currently support the 64-bit version
of the Raspberry Pi OS" ([piwheels FAQ](https://www.piwheels.org/faq.html)).

**32-bit Pi OS and piwheels.** "Raspberry Pi OS includes configuration for
`pip` to use piwheels by default, which lives at `/etc/pip.conf`" with
`[global] extra-index-url=https://www.piwheels.org/simple`
([piwheels.org](https://www.piwheels.org/)); pip "will fall back to PyPI if
the requested package (or one of its dependencies) is not available on
piwheels" ([piwheels FAQ](https://www.piwheels.org/faq.html)). That global
file also applies inside a venv: pip combines configuration "in the following
order: Global, User, Site" where Global is `/etc/pip.conf` and Site is
`$VIRTUAL_ENV/pip.conf`
([pip — configuration](https://pip.pypa.io/en/stable/topics/configuration/)).
piwheels tags are `linux_armv6l` / `linux_armv7l` ("wheels built on a
Raspberry Pi 2/3/4 running the 32-bit OS are tagged `armv7l`" and are
"renamed `armv6l`", [FAQ](https://www.piwheels.org/faq.html)). Coverage of the
two PyPI gaps and the rest, from the piwheels project pages / JSON on
2026-09-02: `pyyaml` 6.0.3 cp311 + cp313 ([piwheels](https://www.piwheels.org/project/pyyaml/));
`cffi` 2.1.1 cp311 + cp313 ([piwheels JSON](https://www.piwheels.org/project/cffi/json/));
`cryptography` 50.0.1 cp311-abi3 + cp313-abi3, with apt dependencies
`libssl3 libatomic1` (cp313 build: `libssl3t64` and others)
([piwheels](https://www.piwheels.org/project/cryptography/),
[JSON](https://www.piwheels.org/project/cryptography/json/)); `rpds-py`
2026.6.3 cp311 + cp313 ([piwheels](https://www.piwheels.org/project/rpds-py/));
`websockets` 17.1 cp311 + cp313 ([piwheels](https://www.piwheels.org/project/websockets/)).
piwheels builds link the system OpenSSL, whereas PyPI's "`cryptography` ships
`manylinux` wheels (as of 2.0) so all dependencies are included"
([cryptography — installation](https://cryptography.io/en/latest/installation/)).

**32-bit userland on a 64-bit kernel.** `arm_64bit` "Defaults to 1 on
Raspberry Pi 4, 400, and Compute Module 4 and 4S platforms", and "Flagship
models since Raspberry Pi 5 ... only support the 64-bit kernel"
([config.txt](https://www.raspberrypi.com/documentation/computers/config_txt.html));
the 32-bit OS "actually has a 64-bit kernel and a 32-bit userland" on those
boards ([raspberrypi.com — Bookworm](https://www.raspberrypi.com/news/bookworm-the-new-version-of-raspberry-pi-os/)).
pip's tag logic handles this: a 32-bit interpreter reporting `linux_aarch64`
is treated as `linux_armv8l` with `armv7l` as a compatible arch
([packaging tags.py](https://raw.githubusercontent.com/pypa/packaging/main/src/packaging/tags.py));
the fix dates from packaging 20.2 ("Fix a bug that caused a 32-bit OS that
runs on a 64-bit ARM CPU ... to report the wrong bitness",
[packaging CHANGELOG](https://raw.githubusercontent.com/pypa/packaging/main/CHANGELOG.rst)),
and pip 23.0.1 (Bookworm's venv pip) vendors packaging 21.3
([pip 23.0.1 vendor.txt](https://raw.githubusercontent.com/pypa/pip/23.0.1/src/pip/_vendor/vendor.txt)),
whose `tags.py` maps `linux_aarch64` to `linux_armv7l` for 32-bit interpreters
([packaging 21.3 tags.py](https://raw.githubusercontent.com/pypa/packaging/21.3/packaging/tags.py)).

**armv6 boards.** The 32-bit image "is designed for older Raspberry Pi
models ... like the original Raspberry Pi, Raspberry Pi 2, and Raspberry Pi
Zero"; the 64-bit image "for newer Raspberry Pi models ... like Raspberry Pi
3, 4, and 5" ([raspberrypi.com — OS](https://www.raspberrypi.com/documentation/computers/os.html)).
PyPI carries no `armv6l` wheels for any dependency in the table; only
piwheels' `linux_armv6l` builds serve those boards.

**Without a wheel.** "Building `cryptography` requires having a working Rust
toolchain"; "The current minimum supported Rust version is 1.83.0"; "The
Rust available in Debian versions prior to Trixie are older than the minimum
supported version"; Debian/Ubuntu build prerequisites `build-essential
libssl-dev libffi-dev python3-dev cargo pkg-config`
([cryptography — installation](https://cryptography.io/en/latest/installation/)).
Bookworm's `rustc` is `1.63.0+dfsg1-2`, trixie's `1.85.0+dfsg3-1`
([bookworm](https://packages.debian.org/bookworm/rustc),
[trixie](https://packages.debian.org/trixie/rustc)). `cffi` from source needs
`python-dev` and `libffi-dev`
([cffi — installation](https://cffi.readthedocs.io/en/latest/installation.html)).
`pyyaml`'s sdist tries the libyaml extension and logs "Error compiling module,
falling back to pure Python" on failure
([pyyaml setup.py](https://raw.githubusercontent.com/yaml/pyyaml/main/setup.py)).
`websockets` has a `py3-none-any` wheel, so no compiler is ever needed for
it. Build-time reference points: piwheels' "longest compile time for a
successful build is currently over 3 hours" ([FAQ](https://www.piwheels.org/faq.html));
a 2021 report of "35 minutes of compiling `cryptography` -- a process which
used to take seconds -- with no end in sight" on a Pi when Rust became
mandatory ([pyca/cryptography #5861](https://github.com/pyca/cryptography/issues/5861),
anecdotal).

**Per mechanism.** Identical. A, B and C each hand pip the same requirement
closure and pip resolves it from the same indexes (PyPI plus the preconfigured
piwheels extra index). The only package pip *builds* differently is ours:
A installs it editable from the checkout, C builds a wheel from the cloned
`subdirectory` ("the project is built locally in a temp dir and then
installed normally", [pip — VCS support](https://pip.pypa.io/en/stable/topics/vcs-support/)),
B downloads the published wheel — all pure Python, no compiler. One resolution
detail differs: under C, `blaueis-core` is an ordinary dependency and is
fetched from the index, not from the cloned repository ("Pip looks for
packages in a number of places: on PyPI (or the index given as
`--index-url` ...), in the local filesystem, and in any additional repositories
specified via `--find-links` or `--extra-index-url`",
[pip install](https://pip.pypa.io/en/stable/cli/pip_install/)), unless it is
also given as a direct URL.

## 3. Shipping systemd units and a CLI in a wheel

**What the wheel format can place where.** On install, "Each subdirectory of
`distribution-1.0.data/` is a key into a dict of destination directories, such
as `distribution-1.0.data/(purelib|platlib|headers|scripts|data)`", mapped to
the sysconfig install paths
([binary distribution format](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)).
Inside a venv those paths are under the venv prefix, so a unit shipped via
`data` lands at e.g. `/opt/blaueis-gw/venv/lib/systemd/system/...`, never at
`/etc/systemd/system`.

**setuptools' position.** The `[tool.setuptools]` `data-files` key is
"**Discouraged** - check Data Files Support. Whenever possible, consider using
data files inside the package directories"
([setuptools — pyproject config](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html));
egg-style data files are deprecated and "pip-based installs fall back to the
platform-specific location for installing data files"; the recommendation is
that "any data files you wish to be accessible at run time be included
**inside the package**" and read with `importlib.resources`
([setuptools — data files](https://setuptools.pypa.io/en/latest/userguide/datafiles.html)).
`include-package-data` is "True by default (only when using pyproject.toml
project metadata/config)" ([pyproject config](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html));
`blaueis-core` already uses `[tool.setuptools.package-data]` this way.

**Console scripts.** `[project.scripts]` declares commands; "after installing
your project, a `spam-cli` command will be available"
([writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)).
Installers "are expected to set up wrappers for both `console_scripts` and
`gui_scripts` in the scripts directory of the install scheme", recorded in
`entry_points.txt` in `*.dist-info`
([entry points spec](https://packaging.python.org/en/latest/specifications/entry-points/)).
In a venv that is `venv/bin/`, not on a stranger's `PATH` — today's
`install.sh` already symlinks the bash CLI into `/usr/local/bin` for the same
reason. `blaueis-configure` is already such an entry point.

**How comparable daemons do it.**

- *Home Assistant Core* (venv + `pip install homeassistant`): the unit was
  never installed by pip; the community guide supplies
  `/etc/systemd/system/home-assistant@YOUR_USER.service` with
  `ExecStart=/srv/homeassistant/bin/hass -c "/home/%i/.homeassistant"`,
  `User=%i` ([community guide](https://community.home-assistant.io/t/autostart-using-systemd/199497)).
  The Core method ("where you run your system in a Python environment") is
  deprecated: notices from 2025.6, support ends with 2025.12, "References to
  these installation methods will be removed from our documentation", and
  32-bit ARM is deprecated alongside
  ([home-assistant.io blog](https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/)).
- *OctoPrint* (venv + `pip install octoprint`): the repository has no
  `scripts/octoprint.service` on `master` or `dev` (HTTP 404 on 2026-09-02),
  and PR "Add systemd unit file" is unmerged
  ([OctoPrint/OctoPrint#1716](https://github.com/OctoPrint/OctoPrint/pull/1716));
  the unit comes from the community setup guide, not from the package (the
  guide page could not be fetched from here — see Gaps).
- *ESPHome*: `pip install esphome`; the docs note "Many Linux distributions
  now refuse to `pip install` into the system Python" and offer a desktop app
  and Docker instead; no systemd guidance at all
  ([esphome.io — installing](https://esphome.io/guides/installing_esphome)).
- *Moonraker*: `git clone` + `scripts/install-moonraker.sh`; the script
  creates the venv, pip-installs, and writes the unit itself with
  `sudo /bin/sh -c "cat > ${SERVICE_FILE}"` into `/etc/systemd/system`, then
  `systemctl enable`; updates go through its git-based `update_manager`
  ([Moonraker — installation](https://moonraker.readthedocs.io/en/latest/installation/),
  [install-moonraker.sh](https://raw.githubusercontent.com/Arksine/moonraker/master/scripts/install-moonraker.sh)).
  The docs do not mention PyPI.

None of the four delivers a unit through pip; each uses a repo-side script
(Moonraker) or a documentation-supplied file (HA, OctoPrint).

**Per mechanism.** Differs. A has `packages/blaueis-gateway/systemd/` and
`scripts/` in the checkout and today's installer copies them. B and C install
only what is under `src/` plus metadata; to reach `/etc/systemd/system` and
`/usr/local/bin` they need either (i) the units as package data plus a
console script that writes them with root, or (ii) a separate download of
those files — i.e. a bootstrap script of the same kind as today's.

## 4. Version reporting

- `importlib.metadata.version(name)` returns "the installed distribution
  package version for the named distribution package as a string" and raises
  `PackageNotFoundError` when absent; distribution names "are *not*
  necessarily equivalent to or correspond 1:1 with the top-level *import
  package* names" (`packages_distributions()` maps them)
  ([docs.python.org — importlib.metadata](https://docs.python.org/3/library/importlib.metadata.html)).
  This works for A (editable install still writes `*.dist-info`), B and C.
- **A** additionally has git: `blaueis-update` uses `git describe --tags
  --always` today. With `git clone --depth 1 --branch v<ver>`, "`--branch`
  can also take tags and detaches the `HEAD` at that commit"; `--depth`
  "Implies `--single-branch`"; tags are cloned by default
  ([git-clone](https://git-scm.com/docs/git-clone)), so `describe` resolves
  the release tag. The `safe.directory` dance in `install.sh` remains
  necessary whenever the invoking user differs from the owner.
- **B** has no VCS metadata at all: `direct_url.json` "MUST NOT be created
  when installing a distribution from an other type of requirement" than a
  direct URL ([direct URL spec](https://packaging.python.org/en/latest/specifications/direct-url/)).
- **C** gets `direct_url.json` in `*.dist-info` with `url`, `vcs_info`
  (`vcs: "git"`, `requested_revision` = the tag, `commit_id` = the resolved
  SHA) and a top-level `subdirectory`
  ([direct URL data structure](https://packaging.python.org/en/latest/specifications/direct-url-data-structure/)).
  pip resolves the tag to a commit at install time ("Resolved %s to commit
  %s", [pip vcs/git.py](https://raw.githubusercontent.com/pypa/pip/main/src/pip/_internal/vcs/git.py)).
  A's editable install writes `dir_info: {"editable": true}` with a
  `file://` URL instead (same spec).
- The `[project] version` in all three `pyproject.toml` files is static
  (`"0.1.0"`); `blaueis-core` lists `setuptools-scm` in `build-system.requires`
  without declaring `dynamic = ["version"]`, so the git tag does not feed the
  metadata version today. Static and tag must be kept in step by the release
  process under every mechanism. (`dynamic` "allows use cases such as filling
  the version from a `__version__` attribute or a Git tag",
  [writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/).)

## 5. Update and rollback semantics

**Resolving "latest release" without extra tooling.**

- GitHub Releases API: `GET /repos/{owner}/{repo}/releases/latest` returns
  "The most recent non-prerelease, non-draft release, sorted by the
  `created_at` attribute"; released information is available to everyone
  ([docs.github.com — releases](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)),
  at 60 unauthenticated requests per hour per IP
  ([rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)).
  Needs `curl` (in the Lite package list) or Python's `urllib`.
- `git ls-remote --tags --sort=-v:refname <url>` "Displays references
  available in a remote repository" without a local repository; annotated
  tags print a second, peeled `^{}` line; `--sort=-v:refname` treats names as
  versions ([git-ls-remote](https://git-scm.com/docs/git-ls-remote)). Needs
  `git`.
- PyPI JSON: `GET /pypi/<project>/json` — `info.version` is the latest
  version, `urls[]` lists files with `digests` and `yanked`
  ([docs.pypi.org — JSON API](https://docs.pypi.org/api/json/)). `pip index
  versions <pkg>` prints "Available versions: ..."
  ([pip index](https://pip.pypa.io/en/stable/cli/pip_index/)). Needs nothing
  beyond the venv.

**A — checkout of the previous tag.** A `--depth 1` clone holds only the
tagged commit. Fetching another tag: "`tag <tag>` means the same as
`refs/tags/<tag>:refs/tags/<tag>`; it requests fetching everything up to the
given tag" (`git fetch origin tag v1.0`); with `--depth` on a shallow repo git
will "deepen or shorten the history to the specified number of commits. Tags
for the deepened commits are not fetched"; `--unshallow` converts to a
complete clone ([git-fetch](https://git-scm.com/docs/git-fetch)). So a
rollback is offline only if the previous tag was fetched beforehand;
otherwise it is a network fetch of that tag's objects, followed by `pip
install -e` again (pure Python, no downloads if dependencies are unchanged).
Today's `blaueis-update --rollback` does `git reset --hard HEAD~1`, which
presumes `main`-tracking history, not tags.

**B — `pip install blaueis-gateway==<prev>`.** pip installs "while
uninstalling any being upgraded or replaced"
([pip install](https://pip.pypa.io/en/stable/cli/pip_install/)) and "also
performs an automatic uninstall of an old version of a package before
upgrading" ([user guide](https://pip.pypa.io/en/stable/user_guide/)); a pinned
`==` requirement is the documented form (`SomePackage==1.0.4 # specific
version`). Offline: pip caches "HTTP responses" and "Locally built wheels" in
`~/.cache/pip`, and "if there is a cached wheel for the same version of a
specific package name, pip will use that wheel"
([pip — caching](https://pip.pypa.io/en/stable/topics/caching/)) — but the
documented offline route is `pip download` into a directory that "can later
be passed as the value to `pip install --find-links` to facilitate offline or
locked down package installation", combined with `--no-index`
([pip download](https://pip.pypa.io/en/stable/cli/pip_download/),
[pip install](https://pip.pypa.io/en/stable/cli/pip_install/)). The pip
cache alone is not a guaranteed offline rollback.

**C — `pip install "... @ git+...@v<prev>#subdirectory=..."`.** Same
uninstall/replace semantics as B, but pip must clone again: it runs `git
clone --filter=blob:none` (git >= 2.17) and then `checkout` of the requested
rev ([pip vcs/git.py](https://raw.githubusercontent.com/pypa/pip/main/src/pip/_internal/vcs/git.py)),
and only "caches wheels when building from an immutable Git reference (i.e. a
commit hash)" ([pip — caching](https://pip.pypa.io/en/stable/topics/caching/))
— a tag is not immutable to pip, so every install or rollback by tag is a
fresh clone and build. Offline rollback is therefore not available under C
unless the reference is a commit SHA and the wheel is already cached.

**Per mechanism.** Differs on every axis: tool needed (A git; B none; C
git), what "latest" is read from (A/C: tags or Releases API; B: PyPI), and
offline rollback (A: yes when the old tag's objects are local; B: only with a
pre-downloaded wheelhouse; C: no).

## 6. Supply chain and integrity

**PyPI attestations (B).** PEP 740 attestations are "cryptographically
bound" to a distribution's filename and SHA-256 digest; the index may serve a
provenance object per file via the `data-provenance` link on the Simple index;
the PEP "does not make a policy recommendation around mandatory digital
attestations on release uploads or their subsequent verification by installing
clients like `pip`" ([PEP 740](https://peps.python.org/pep-0740/)). With
`pypa/gh-action-pypi-publish` and Trusted Publishing "attestations are
generated and uploaded automatically by default"; supported publishers are
GitHub Actions, GitLab CI/CD and Google Cloud
([producing attestations](https://docs.pypi.org/attestations/producing-attestations/)).
"PyPI only permits attestations with a verifiable signature to be uploaded";
an attestation gives "a strong and verifiable association between a file on
PyPI and the source repository, workflow, and even the commit hash that
produced and uploaded the file"
([PyPI blog, 2024-11-14](https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/)).
Verification today is manual: the `pypi-attestations` CLI downloads the file
and "its corresponding provenance JSON (using the Integrity API)", checks the
Trusted Publisher against `--repository`, and "cryptographically verifies the
wheel" ([consuming attestations](https://docs.pypi.org/attestations/consuming-attestations/));
endpoint `GET /integrity/<project>/<version>/<filename>/provenance`
([Integrity API](https://docs.pypi.org/api/integrity/)). No primary pip
document states native verification; a third-party pip plugin exists that
"verifies PEP-740 attestations before installing a package, and aborts the
installation if verification fails"
([trailofbits/pip-plugin-pep740](https://github.com/trailofbits/pip-plugin-pep740),
secondary). For a first release, a "pending" publisher "does **not** create a
project or reserve a project's name **until** it is actually used to publish"
and becomes a normal trusted publisher on first use
([creating a project through OIDC](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)).
Stranger-side integrity under B without extra tools: TLS to PyPI plus the
`digests` in the JSON API; with `pypi-attestations`: publisher identity.

**Git tags (A, C).** `git tag -s` signs; `git tag -v` verifies only if the
signer's public key is in the verifier's keyring, else "Can't check
signature: public key not found"
([Pro Git — signing](https://git-scm.com/book/en/v2/Git-Tools-Signing-Your-Work)).
A plain tag can move. GitHub immutable releases (GA 2025-10-28) lock the tag:
"locked to a specific commit, cannot be changed, and cannot be deleted while
the release exists", assets "can't be added, modified, or deleted", and a
release attestation "containing the release tag, commit SHA, and release
assets" is generated; enabled per repository or organisation, "all new
releases are immutable"
([immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
[changelog](https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/)).
Verifying that attestation needs the `gh` CLI (`gh attestation verify <file>
-R owner/repo`, online by default)
([artifact attestations](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds)).
Under C, pip additionally records the resolved `commit_id` (section 4), so
what was installed is auditable after the fact; nothing is verified at install
time by pip beyond TLS and the git object hashes.

**The bootstrap step (`curl | bash`).** Today's header fetches
`raw.githubusercontent.com/.../main/scripts/install.sh` — content that changes
with every commit to `main`. pip's own installer guidance is download-then-run
("Download the script, from https://bootstrap.pypa.io/get-pip.py. Open a
terminal/command prompt, cd to the folder containing the get-pip.py file and
run: `python get-pip.py`") with no pipe-to-interpreter form
([pip — installation](https://pip.pypa.io/en/stable/installation/)). GitHub's
own guidance for anything hash-verified is release assets, not generated
archives: "Because branches and tags can move to different commit IDs, future
downloads of an archive may have different contents"; compression may change
with six months' notice; "we recommend using releases instead of using source
downloads" for cryptographically stable downloads
([source code archives](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives),
[github.blog, 2023-02-21](https://github.blog/open-source/git/update-on-the-future-stability-of-source-code-archives-and-hashes/)).
A release asset under an immutable release is fixed bytes with an attestation;
a `main` raw URL is neither. This step is identical for A, B and C as long as a
script sets up the user, `/etc/blaueis-gw`, the units and the CLI.

## 7. Disk and time

Not measured here (the maintainer measures locally); only the levers.

- **Wheel install (B, C).** Only `src/` contents, metadata and the entry
  point wrappers are spread into the venv ([binary distribution format](https://packaging.python.org/en/latest/specifications/binary-distribution-format/));
  no tests, fixtures, docs or `.git`. Under C the clone lives in a temp dir
  and is discarded after the build ([pip — VCS support](https://pip.pypa.io/en/stable/topics/vcs-support/)).
- **`--depth`.** "Create a *shallow* clone with a history truncated to the
  specified number of commits. Implies `--single-branch`"
  ([git-clone](https://git-scm.com/docs/git-clone)). It removes history, not
  the tagged tree: every blob of that tree (test fixtures included) is
  transferred. Today's installer uses `--depth 50`.
- **`--filter=blob:none`.** "will filter out all blobs (file contents) until
  needed by Git" ([git-clone](https://git-scm.com/docs/git-clone)); partial
  clone lets git "avoid downloading such unneeded objects **in advance**" and
  "Missing objects can later be 'demand fetched'"; `checkout` "has been taught
  to bulk pre-fetch all required missing blobs in a single batch"; it
  "requires that the user be online and the origin remote ... be available for
  on-demand fetching", and "Dynamic object fetching tends to be slow as objects
  are fetched one at a time" ([partial clone](https://git-scm.com/docs/partial-clone)).
  `tree:0` omits trees as well ([git-rev-list](https://git-scm.com/docs/git-rev-list)).
  pip's own clone under C is exactly `--filter=blob:none` without `--depth`
  ([pip vcs/git.py](https://raw.githubusercontent.com/pypa/pip/main/src/pip/_internal/vcs/git.py)):
  full commit/tree history, blobs only for the checked-out tree.
- **Sparse checkout.** "change the working tree from having all tracked files
  present to only having a subset of those files" using the skip-worktree
  bit; cone mode takes directories, and "any paths immediately under leading
  directories (including the toplevel directory) will also be included"
  ([git-sparse-checkout](https://git-scm.com/docs/git-sparse-checkout));
  `git clone --sparse` starts "with only files in the toplevel directory
  initially being present" ([git-clone](https://git-scm.com/docs/git-clone)).
  On its own it changes the working tree, not the object store; with
  `--filter=blob:none` only the blobs inside the cone are fetched.
- For A the cheapest well-defined recipe from these documents is `git clone
  --depth 1 --branch v<ver> --filter=blob:none --sparse <url>` followed by
  `git sparse-checkout set packages/blaueis-core packages/blaueis-gateway`
  (plus `scripts` if the installer stays in-repo); an editable install then
  needs nothing outside the cone. Whether `pip install -e` or `git describe`
  triggers further lazy fetches is not verified here.
- **Time.** Dependency download/build time is identical (section 2). A adds
  the clone; C adds pip's clone plus a local wheel build of a pure-Python
  package; B adds one wheel download.

## Closing table

| Constraint | A clone@tag | B PyPI | C pip@git-tag | same/differs |
|---|---|---|---|---|
| PEP 668: venv mandatory, no system-site install | yes | yes | yes | same |
| System Python 3.11 (Bookworm) / 3.13 (Trixie); `>=3.11` floor | yes | yes | yes | same |
| Venv pip version (23.0.1 Bookworm / 25.1.1 Trixie) unless `--upgrade-deps` | yes | yes | yes | same |
| Dependency wheel coverage, piwheels reliance on 32-bit, Rust risk without wheel | identical closure | identical closure | identical closure | same |
| 32-bit userland on 64-bit kernel handled by pip's tag logic | yes | yes | yes | same |
| `git` binary required on the Pi | yes | no | yes | differs |
| Network required at install | yes | yes | yes | same |
| `blaueis-core` source | checkout (editable) | PyPI | PyPI (unless second direct URL) | differs |
| systemd units and bash CLI present after install | in checkout | not in wheel | not in wheel | differs |
| Path to `/etc/systemd/system`, `/usr/local/bin` | installer copies | needs console script + package data or separate download | same as B | differs |
| Version source | metadata + `git describe` | metadata only | metadata + `direct_url.json` (tag, commit) | differs |
| Static `version` must match tag | yes | yes | yes | same |
| "Latest" lookup without extra tooling | `git ls-remote` / Releases API (60/h) | PyPI JSON / `pip index` | `git ls-remote` / Releases API | differs |
| Rollback to previous release | fetch old tag + checkout + `pip -e` | `pip install ==prev` | `pip install @prev` (re-clone, rebuild) | differs |
| Offline rollback | if old tag objects already local | only with `pip download` wheelhouse | no (tag builds not cached) | differs |
| Integrity artefact a stranger can check | git tag (signed / immutable release) | PEP 740 attestation + digests (manual verify) | git tag + recorded commit id | differs |
| pip verifies provenance at install | no | no (pip has no native PEP 740 verification) | no | same |
| Bootstrap script fetched from mutable `main` URL | yes | yes | yes | same |
| Disk: repo tree + `.git` on device | yes (reducible via depth/filter/sparse) | no | no (temp clone) | differs |
| Install time beyond dependencies | clone | wheel download | clone + local build | differs |

## Gaps / open points

- OctoPrint's community setup guide (the source of its `octoprint.service`)
  is behind a bot wall and could not be read; the "no unit in the repo"
  statement rests on the 404s and the unmerged PR only.
- Which Debian package writes `/etc/pip.conf` on Raspberry Pi OS was not
  identified (the `raspberrypi-sys-mods` tree has no such file); the piwheels
  homepage statement is the only source for "preconfigured".
- `git` absent from the Lite image is inferred from one pi-gen package list;
  confirm on a freshly flashed image before relying on it.
- piwheels' project JSON exposes no build durations, so Pi build times for
  `cryptography` remain anecdotal (FAQ maximum, one 2021 issue).
- No primary pip document was found stating whether pip will verify PEP 740
  attestations; PEP 740's non-mandate and the third-party plugin are the
  evidence.
- Whether a `--depth 1 --filter=blob:none --sparse` clone stays free of lazy
  fetches during `pip install -e` and `git describe` is untested.
- Board scope (armv6 Pi Zero / Pi 1 have no PyPI wheels at all; Pi 5 is
  64-bit-kernel only) is a product decision, not a packaging fact.
- The `cryptography` armv7l wheel on PyPI is `manylinux_2_31`; its selection on
  a real 32-bit Bookworm/Trixie Pi (with and without the 64-bit kernel) should
  be confirmed on hardware.

## Sources

- PEP 668 — https://peps.python.org/pep-0668/
- PEP 600 — https://peps.python.org/pep-0600/
- PEP 740 — https://peps.python.org/pep-0740/
- Raspberry Pi OS documentation — https://www.raspberrypi.com/documentation/computers/os.html
- Raspberry Pi config.txt documentation — https://www.raspberrypi.com/documentation/computers/config_txt.html
- Raspberry Pi OS Bookworm announcement — https://www.raspberrypi.com/news/bookworm-the-new-version-of-raspberry-pi-os/
- Raspberry Pi OS Trixie announcement — https://www.raspberrypi.com/news/trixie-the-new-version-of-raspberry-pi-os/
- pi-gen stage2 package list — https://raw.githubusercontent.com/RPi-Distro/pi-gen/master/stage2/01-sys-tweaks/00-packages
- Debian packages: python3 (trixie) https://packages.debian.org/trixie/python3 · python3-venv (bookworm) https://packages.debian.org/bookworm/python3-venv · python3.11-venv https://packages.debian.org/bookworm/python3.11-venv · python3-pip-whl https://packages.debian.org/bookworm/python3-pip-whl · python3-pip (trixie) https://packages.debian.org/trixie/python3-pip · libc6 https://packages.debian.org/bookworm/libc6 , https://packages.debian.org/trixie/libc6 · rustc https://packages.debian.org/bookworm/rustc , https://packages.debian.org/trixie/rustc
- Python docs: venv https://docs.python.org/3/library/venv.html · importlib.metadata https://docs.python.org/3/library/importlib.metadata.html
- piwheels — https://www.piwheels.org/ · FAQ https://www.piwheels.org/faq.html · project pages/JSON for cryptography, cffi, pyyaml, rpds-py, websockets (https://www.piwheels.org/project/<name>/ and /json/)
- PyPI JSON API — https://docs.pypi.org/api/json/ · per-package https://pypi.org/pypi/<name>/json
- PyPI attestations — https://docs.pypi.org/attestations/ · producing https://docs.pypi.org/attestations/producing-attestations/ · consuming https://docs.pypi.org/attestations/consuming-attestations/ · Integrity API https://docs.pypi.org/api/integrity/ · pending publishers https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/ · blog https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/
- trailofbits/pip-plugin-pep740 (secondary) — https://github.com/trailofbits/pip-plugin-pep740
- pip docs: VCS support https://pip.pypa.io/en/stable/topics/vcs-support/ · caching https://pip.pypa.io/en/stable/topics/caching/ · configuration https://pip.pypa.io/en/stable/topics/configuration/ · pip install https://pip.pypa.io/en/stable/cli/pip_install/ · pip download https://pip.pypa.io/en/stable/cli/pip_download/ · pip index https://pip.pypa.io/en/stable/cli/pip_index/ · user guide https://pip.pypa.io/en/stable/user_guide/ · installation https://pip.pypa.io/en/stable/installation/
- pip source: vcs/git.py https://raw.githubusercontent.com/pypa/pip/main/src/pip/_internal/vcs/git.py · 23.0.1 vendor.txt https://raw.githubusercontent.com/pypa/pip/23.0.1/src/pip/_vendor/vendor.txt
- packaging: tags.py https://raw.githubusercontent.com/pypa/packaging/main/src/packaging/tags.py · 21.3 tags.py https://raw.githubusercontent.com/pypa/packaging/21.3/packaging/tags.py · CHANGELOG https://raw.githubusercontent.com/pypa/packaging/main/CHANGELOG.rst
- packaging.python.org: entry points https://packaging.python.org/en/latest/specifications/entry-points/ · direct URL https://packaging.python.org/en/latest/specifications/direct-url/ · direct URL data structure https://packaging.python.org/en/latest/specifications/direct-url-data-structure/ · binary distribution format https://packaging.python.org/en/latest/specifications/binary-distribution-format/ · writing pyproject.toml https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- setuptools: data files https://setuptools.pypa.io/en/latest/userguide/datafiles.html · pyproject config https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html
- cryptography installation — https://cryptography.io/en/latest/installation/ · issue #5861 https://github.com/pyca/cryptography/issues/5861
- cffi installation — https://cffi.readthedocs.io/en/latest/installation.html
- pyyaml setup.py — https://raw.githubusercontent.com/yaml/pyyaml/main/setup.py
- Home Assistant: Core deprecation https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/ · community systemd guide https://community.home-assistant.io/t/autostart-using-systemd/199497
- OctoPrint PR #1716 — https://github.com/OctoPrint/OctoPrint/pull/1716
- ESPHome installation — https://esphome.io/guides/installing_esphome
- Moonraker: installation https://moonraker.readthedocs.io/en/latest/installation/ · install script https://raw.githubusercontent.com/Arksine/moonraker/master/scripts/install-moonraker.sh
- git docs: git-clone https://git-scm.com/docs/git-clone · git-fetch https://git-scm.com/docs/git-fetch · git-ls-remote https://git-scm.com/docs/git-ls-remote · partial clone https://git-scm.com/docs/partial-clone · git-sparse-checkout https://git-scm.com/docs/git-sparse-checkout · git-rev-list https://git-scm.com/docs/git-rev-list · Pro Git signing https://git-scm.com/book/en/v2/Git-Tools-Signing-Your-Work
- GitHub docs: latest release https://docs.github.com/en/rest/releases/releases#get-the-latest-release · rate limits https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api · immutable releases https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases · changelog https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/ · source archives https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives · archive stability https://github.blog/open-source/git/update-on-the-future-stability-of-source-code-archives-and-hashes/ · artifact attestations https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds
- In-repo: `scripts/install.sh`, `packages/blaueis-gateway/scripts/blaueis-update`, `packages/blaueis-gateway/systemd/`, `packages/*/pyproject.toml`
