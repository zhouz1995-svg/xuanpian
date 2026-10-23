# Offline model provenance

The current source tree contains two official ONNX files. Runtime loading checks
their SHA-256 digests before creating CPU inference sessions. There is no runtime
network download. Restore files with `scripts/vendor_analysis_models.py`, which
checks the pinned digest and length before replacement.

| Asset | Upstream identity | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| yunet.onnx | OpenCV Zoo commit 48b10fa505aeb51fbd127fc60a5c660040e823b7, face_detection_yunet_2023mar.onnx | 232589 | 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 |
| yolox_nano.onnx | Official Megvii YOLOX release 0.1.1rc0, yolox_nano.onnx | 3659407 | c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d |

Exact download URLs, versions and upstream license links are in the
[model manifest](../src/xuanpian/resources/models/manifest.json).

## License evidence

YuNet's [official model directory](https://github.com/opencv/opencv_zoo/blob/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/README.md)
states that all files in that directory are MIT licensed. The verbatim text is
preserved at [YuNet-MIT.txt](../packaging/licenses/models/YuNet-MIT.txt).

YOLOX-Nano is downloaded directly from the project's
[official release](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0).
The project's Apache-2.0 text from the release tag is preserved at
[YOLOX-Apache-2.0.txt](../packaging/licenses/models/YOLOX-Apache-2.0.txt).
No separate artifact-specific model card was found. The inventory records this
as partial evidence rather than presenting repository licensing as a separate,
explicit weight-specific grant.

No local ONNX export is performed. Runtime users do not need PyTorch or ONNX
conversion tools. The release notes explicitly remove RGB normalization for
the updated weights; these exact assets use raw 0..255 BGR input, top-left
letterboxing with value 114, and external stride-grid decoding. The old tag's
demo source is inconsistent with the release assets and must not be substituted
for this tested preprocessing.

The previous BlazeFace, Pose Landmarker and EfficientDet blobs remain in Git
history but are absent from the current package. Their historical audit rows
are labeled `historical_model`, not current bundled models.
