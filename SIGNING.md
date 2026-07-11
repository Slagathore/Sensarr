# Release signing

Release executables (`Plexxarr.exe` in the zip, `Plexxarr-portable.exe`) are
Authenticode-signed (publisher `CN=Charles Chambers`) using Azure Artifact
Signing, timestamped so signatures outlive the short-lived certificates.

Signing happens locally after the PyInstaller build, before assets are
zipped/uploaded. The account details, environment workarounds, and the exact
`Invoke-TrustedSigning` invocation live in the private signing playbook
(`CODE-SIGNING-PLAYBOOK.md`, kept outside this repo; reference implementation
in the `job_finder_v2` repo). Verify any release binary with:

```powershell
Get-AuthenticodeSignature .\Plexxarr.exe | Format-List Status, SignerCertificate
# Expect: Status Valid, CN=Charles Chambers
```
