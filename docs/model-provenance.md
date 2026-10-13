# Bundled model provenance

This record captures the evidence reviewed on 2026-10-13 for the three model
files shipped with XuanPian. It is an artifact-specific provenance record, not
legal advice or a clearance of the rest of the application dependency graph.

## Identity method

Each checked-in file was compared with its fixed version-1 object in Google's
public `mediapipe-models` bucket. Local SHA-256 is the project's integrity key;
the byte count and base64 MD5 below matched the corresponding Google Cloud
Storage object metadata and response headers. The GCS generation identifies the
immutable object revision observed during review.

| Project file | Fixed upstream object | Bytes | GCS generation | GCS/local MD5 (base64) | Local SHA-256 | Result |
| --- | --- | ---: | --- | --- | --- | --- |
| `face_detector.tflite` | [BlazeFace short-range float16 v1](https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite) | 229746 | `1682480001338381` | `o91uwxclKQdwuXzsDL+UyQ==` | `b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f` | Exact object identity verified |
| `pose_landmarker.task` | [Pose Landmarker Lite float16 v1](https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task) | 5777746 | `1682624736756847` | `BKdd33yBGsehpFIyZt19iA==` | `59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a` | Exact object identity verified |
| `object_detector.tflite` | [EfficientDet-Lite0 float32 v1](https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float32/1/efficientdet_lite0.tflite) | 13836895 | `1682636012161435` | `ymabHfDr6add3N5X/geg0A==` | `40338edf5ec70d43e318b0a716a84d4564cd1802759a7a07170c7e43796dbf58` | Exact object identity verified; exact grant unresolved |

## License evidence and decision

The official [Face Detector task page](https://developers.google.com/edge/mediapipe/solutions/vision/face_detector)
links the BlazeFace short-range download and the official
[BlazeFace short-range model card](https://storage.googleapis.com/mediapipe-assets/MediaPipe%20BlazeFace%20Model%20Card%20%28Short%20Range%29.pdf).
That model card identifies the model as Apache License 2.0. Because the fixed
download object also matches the checked-in file, `face-detector` is recorded as
`reviewed` under `Apache-2.0`.

The official [Pose Landmarker task page](https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker)
identifies the Lite float16 bundle and links the official
[BlazePose GHUM 3D model card](https://storage.googleapis.com/mediapipe-assets/Model%20Card%20BlazePose%20GHUM%203D.pdf).
The card covers Lite, Full, and Heavy variants and identifies them as Apache
License 2.0. Because the fixed Lite bundle matches the checked-in file,
`pose-landmarker` is recorded as `reviewed` under `Apache-2.0`.

The official [Object Detector task page](https://developers.google.com/edge/mediapipe/solutions/vision/object_detector)
identifies EfficientDet-Lite0 float32 and links its Google-hosted download. The
fixed object matches the checked-in file. An official TensorFlow/Kaggle
EfficientDet Lite0 metadata variation is labelled Apache 2.0, but the downloaded
variation is not byte-identical to this float32 MediaPipe object. No official
model card or other grant explicitly tied to this exact 13,836,895-byte object
was located during this review. `object-detector` therefore remains
`unresolved`; the project does not infer its license from a related variation.

The preserved Apache-2.0 text used for the two reviewed models is
`packaging/licenses/wheels/8707eef0533987efc5b155d64761eeb6e20793f50b9bd1a68dad1cf4719d0ed8.txt`,
originally extracted byte-for-byte from the pinned MediaPipe wheel. Its reuse
avoids adding a second, differently normalized copy of the same license text.

## Remaining boundary

This review does not resolve training-data rights, patents, privacy or biometric
law, suitability for surveillance or identity recognition, trademark rights,
or the licenses and source duties of native/runtime dependencies. The public
release remains blocked while the exact EfficientDet-Lite0 float32 grant and the
other dependency gates remain unresolved.
