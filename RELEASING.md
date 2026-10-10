# Cutting a release

The proven pipeline (first run: v1.2, 2026-07-11). Order matters: sign
before zipping, and never zip a staged folder (staging injects the local
`.env`).

1. **Bump** `APP_VERSION` in `config.py`. Installed apps compare release tags
   against it, and an unbumped version means nobody gets notified.
2. **Test + push**: `python -m pytest tests -q`, commit, push, wait for the
   GitHub check to go green.
3. **Build both flavors**:
   - `build_exe.bat` → `dist\<timestamp>\Sensarr\` (folder pack). If Inno
     Setup (`ISCC.exe`) is installed, this also builds the installer at
     `packaging\Output\Sensarr-<ver>-Setup.exe` (skipped silently if Inno
     Setup isn't present).
   - `python -m PyInstaller Sensarr-portable.spec --noconfirm --distpath dist\portable`
4. **Sign** `Sensarr.exe` (in the raw dist folder), `Sensarr-portable.exe`,
   and `Sensarr-<ver>-Setup.exe` per the private signing playbook: one
   `Invoke-TrustedSigning` call, paths comma-joined in a single `-Files`
   string. Verify all three: `Get-AuthenticodeSignature` → `Valid`,
   `CN=Charles Chambers`.
5. **Assemble the zip** from the RAW build folder (never from a staged one):
   bundle + `anime_meta.sqlite` + `.env.example` + `setup_autostart.bat` +
   `start_sensarr.bat` + `remove_autostart.bat` + `LICENSE` →
   `Sensarr-v<ver>-windows.zip` (the
   name pattern the 1.3.x releases actually shipped).
   For the 1.4.x transition releases: also drop a second copy of the signed
   `Sensarr.exe` into the zip's bundle folder named `Plexxarr.exe`. The 1.3.x
   in-app updater looks for that filename inside the zip and aborts the whole
   update without it (the Authenticode signature stays valid under any
   filename). Release notes should tell people to re-run `setup_autostart.bat`
   once so autostart points at `Sensarr.exe`. Retire the extra copy when no
   1.3.x installs remain.
6. **Sweep the zip**: list entries and fail on any `.env`, `*.db`, `*.pkl`,
   `*.pid`, or non-anime `.sqlite`.
7. **Tag + publish**: `git tag -a v<ver>`, push the tag, `gh release create`
   with the zip + portable exe + installer exe. GitHub attaches source
   zip/tar.gz itself.
8. **Emergency releases**: put a line starting `SENSARR-URGENT: <message>`
   in the release notes; installed apps show it as a red banner that
   ignores dismiss/mute settings. While 1.3.x installs are still out there,
   repeat the same message on a `PLEXXARR-URGENT:` line; old builds only
   scan for their own marker.
9. **Locally**: `python stage_build.py` re-stages the new build with the
   local `.env`, and running `setup_autostart.bat` (elevated) repoints the
   autostart task at it.

## Anime DB workflow keepalive

`.github/workflows/anime-db.yml` (Task I) rebuilds and republishes the
weekly anime metadata artifact on a Monday cron, under the rolling
`anime-db-latest` tag (never marked "latest" release — that stays on the
app's own version tag, see the workflow's header comment). GitHub disables a
scheduled workflow automatically after 60 days with no repository activity.
If the repo goes quiet that long: open the Actions tab, find "anime-db" in
the left sidebar, and click "Enable workflow" (a `workflow_dispatch` run by
hand does NOT re-enable a disabled cron by itself). A normal `git push` to
master before the 60-day mark keeps the schedule alive with no action
needed.
