# 横版精简模式验证

在 pipeline 2.4 中增加横版正反面模板，入口仍为 `--extract-mode endpoints`。验证环境为本机 RapidOCR CPU、4 线程，不使用远程 OCR 或 VLM。

## 验收样本

样本目录：`E:/BaiduNetdiskDownload/底板/1-10`。其中横版 28 张，覆盖安里、敖里、艾默生路和阿尔卡特（白湖亭）的 14 组正反面，包含紫底、绿底、黄色招呼站，以及 2954×1832、2954×1831、1969×1221 三种尺寸。

人工核对原图建立 `tests/fixtures/landscape_expected.json`，记录每张图片的站名、方位、线路号及有序首末站。涉及 141、171、82、142、通学专线13、接驳6、77 等线路。其中：

- 同一线路的相反走向分别验证，不按方位猜测首末站。
- 77 路的“仓山科技园（浦下村委会）”验证两端附注。
- 171 路验证黄色说明栏不会合并站名列。
- 通学专线13验证不均匀竖排字符仍按从上到下读取。
- 接驳6验证长线路标题统一名称并保留实际 OCR 候选。

首次试跑暴露出紧裁超大线路字的误检，以及直接重排竖排字造成的错序。最终实现先将线路字转成黑字白底、缩放并加留白；只重排间隔清楚且字高接近的竖排字，其余按原竖排识别。竖排附注的 ︵ / ︶ 在重排时旋转为普通括号。

## 验收结果

- 批处理完成 28 张，失败 0 张；站名、方位、线路号及 56 个有序端点与人工基准逐项比较，最终差异为 0。
- 西向 171 路反面的线路号 OCR 存在歧义，文件名提示保留为 171，同时保留 1 个 `route_number_uncertain` 复核项，不将提示当作 OCR 确认。
- 一次无缓存的 28 张批处理共 87 张 OCR 输入、56 次 backend 调用，耗时 249.502 秒；与其他验证任务并行运行，不能将该耗时当作固定吞吐。
- 修正完整站名末尾括号的归一化后，无缓存重跑了 77 路两张正面；导出和缓存已更新。最终可查看 `output/landscape-final/station_routes.csv`、`validation_summary.json` 和复核队列。`full_run_report.json` 保存整批运行计数，当前 `batch_report.json` 为重跑记录。
- 完整自动回归：**127 passed, 4 skipped**（221.91 秒）。慢速真实 OCR 与全目录版面测试采用显式开关；横版真实 OCR 另外单独执行。

## 回归覆盖

自动化测试覆盖正反面、三种底色、不同尺寸、反向箭头、箭头不明确、首站/末站附注、空端点、线路前缀、线路号冲突、版块筛选、缓存续跑、批处理导出，以及未知横版和完整模式的既有处理。

另有回归确保多个字粘成大小相近的竖排片段时不会被当成单字重排，以及“仓山科技园（浦下村委会）”这类完整名称不会丢失末尾括号。

完整回归：

```powershell
$env:PYTHONUTF8="1"
$env:BUS_OCR_SAMPLE_ROOT="E:/BaiduNetdiskDownload/底板/1-10"
python -m pytest -o addopts="" -q
```

真实 OCR 回归（读取本地样本，不上传）：

```powershell
$env:RUN_REAL_LANDSCAPE_OCR="1"
python -m pytest tests/test_landscape.py::test_real_landscape_endpoints -o addopts="" -q
```

批处理验收：

```powershell
python -m buspipe run "E:/BaiduNetdiskDownload/底板/1-10" --out output/landscape-final --extract-mode endpoints --only-station 安里 --only-station 敖里 --only-station 艾默生路 --only-station "阿尔卡特（白湖亭）" --threads 4
```

同一目录里的另外 20 张竖版不在这次横版验收筛选中。全部竖版仍由原流程处理，既有回归继续执行。

## 使用范围

匹配布局后，每张横版独立提取一条线路的首末站，结果包含原图坐标和来源，不需要相邻图片才能工作。对于导乘地图、广告及未匹配的其他横版布局，仍明确标记为不支持；不能将所有横图的长宽比视作可识别模板。
