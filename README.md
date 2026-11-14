# Seetong 区域监控

在 Windows 上独立预览 Seetong 云端摄像头，检测选定区域的人数，并在人数稳定增加时保存截图。支持三路通道，单次连接选择一路。

## 启动

需要本机安装 Seetong 1.0.12.3、64 位 Python，以及在 PATH 中可用的 FFmpeg。Python 环境需要 `numpy`、`onnxruntime` 和 `Pillow`。

```powershell
python -m pip install numpy onnxruntime Pillow
python .\outputs\seetong-monitor.py
```

也可以双击 `outputs\启动监控.cmd`。浏览器访问 `http://127.0.0.1:8765/`。登录后选择通道并打开预览，在画面里拖出矩形，然后点击「启动检测」。

Windows DPAPI 加密的账号和密码、每路检测区域以及最近 100 张截图保存在 `outputs/seetong-data/`。清除已保存的账号可在页面上操作。关闭启动窗口即可停止本机服务。

人体检测使用 [OpenCV Zoo YOLOX INT8 模型](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox)。下载下方的模型文件并保存为 `outputs/seetong-yolox.onnx`。本地取流依赖已安装的原厂 Seetong DLL。

[下载 object_detection_yolox_2022nov_int8.onnx](https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_yolox/object_detection_yolox_2022nov_int8.onnx)
