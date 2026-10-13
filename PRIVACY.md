# Privacy and local data

XuanPian performs photo analysis on the device with bundled models and its bundled metadata runtime. The app does not require an account or cloud image processing and does not intentionally upload imported photos or analysis results. Build-time dependency downloads are separate from runtime use.

Imported photo bytes remain in the locations the user chooses. Analysis reads photos and metadata and stores review state, cache data, and project information locally. These may include filenames, paths, metadata-derived values, thumbnails, or user decisions; treat the local project database and generated reports as private. No telemetry claim has been independently established for every bundled third-party component, so the final bundle must be checked before a public privacy assurance is made.

Analysis does not rewrite original photos. An explicit move operation can relocate selected files; an explicit export can write new files to the chosen destination. Confirm the selection and destination and keep your own backups. Deleting the app does not automatically remove your photos or all local project data.

For bug reports, redact personal paths, EXIF (including location/camera serial data), project databases, and images. Use synthetic examples where possible. Do not publish a private photo to demonstrate a focus or AF issue.
