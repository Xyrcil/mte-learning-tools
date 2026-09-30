# MTE Learning Tools

Small Python tools for Android package assembly and local protocol testing.
The project is for study and reference. It is not a game client or a release
package.

## Contents

- `scripts/android-embed/` contains ABI filtering and APK merge helpers.
- `server/` contains the local Python service and its focused tests.
- `.gitignore` keeps local packages, resources, logs, and keys out of commits.

## Test

```sh
python3 -m unittest discover -s scripts/android-embed -p 'test_*.py'
python3 -m compileall -q server
```

The service reads its data and protocol catalog from paths supplied by the
caller. Keep those inputs outside this repository.

Use only files that you have permission to process. Do not add personal data,
credentials, proprietary packages, or third-party resources.
