# 只提取站点与线路走向

`--extract-mode endpoints` 只识别线路号、首站和末站。当前站名、方位来自图片文件名，不需要 OCR。默认 `full` 保持原来的完整识别流程。

例如 `安平小区-东-01.jpg` 的 6 路输出：

```json
{
  "station": "安平小区",
  "direction": "东",
  "station_key": "安平小区-东",
  "route": "6",
  "first_stop": "金山浦上公交枢纽站",
  "last_stop": "公交仁德站",
  "route_direction": "金山浦上公交枢纽站→公交仁德站"
}
```

`station_key` 标识当前站点的方位，首末站组成有顺序的线路走向。甲→乙和乙→甲分别保留。同一张牌上的多条线路分别输出；拆成多张图片的同一站点按相同 `station_key` 关联，每行保留来源图片及版块编号。

## 运行

在 `E:/BaiduNetdiskDownload/bus_ocr` 内运行，先用少量图片验证：

```powershell
python -m buspipe run "E:/BaiduNetdiskDownload/底板/1-10/安平小区" --out "output/endpoints-sample" --extract-mode endpoints --threads 4
```

整目录运行（会开始实际 OCR）：

```powershell
python -m buspipe run "E:/BaiduNetdiskDownload/底板" --out "output/endpoints" --extract-mode endpoints --threads 4
```

中断后重跑同一命令即可。全量与精简模式使用不同的输出目录、工作目录及 manifest，避免已完成状态跳过新模式、或人工修正的序号混用。尝试混用时会给出错误提示。

旧入口也可指定：

```powershell
$env:BUS_SRC="E:/BaiduNetdiskDownload/底板/1-10/安平小区"
python extract.py --poster 安平小区-东-01 --extract-mode endpoints --out output/endpoints-single.json
```

批处理生成：

- `station_routes.csv` / `station_routes.json`：当前站名、方位、站点键、线路号、有序首末站、来源及 `needs_review`。
- `posters/*_raw.json`：端点坐标、OCR 候选、置信度等，可追溯复核。
- `review/待人工复核.csv`：空文字、低置信度、跨列、线路号不确定等异常。
- `batch_report.json` / `batch_report.md`：模式、耗时、调用数量和失败项。

## 识别与复核规则

本地仍对完整图片做几何定位，但只把线路号和两端站名列送入 OCR。首末站附注按整行列宽和间距分组保留；边缘日期或“执行”说明会跳过，再尝试相邻列。普通中间站、票价、时间、公司等不参与 OCR。

正常线路需要 3 张线路号裁片及 1 张端点画布；发生空结果、跨列或低覆盖时，仍允许端点单列回退。端点之间至少留 100px 白边，减少短站名被检测成一行的情况。

读不清的端点保留为空并进入复核，不用中间站替代。`needs_review=true` 的记录仍是待核实候选。配置 VLM 后只复核异常端点，不调用 VLM 识别整张牌。

原始 JSON 的 `endpoints` 独立于完整站序的 `stops`，不会声称“整条线路只有两站”。人工修正沿用 `override` 命令，但精简模式的 `SEQ=1` 表示首站、`SEQ=2` 表示末站。完整站序的下站比对、双向站序相似度检查在本模式中跳过。

## 横版正反面

同一条 `--extract-mode endpoints` 命令会自动选择竖版或横版模板。已适配样本中的紫底、绿底及黄色招呼站单线路横版，包含正面和反面两种布局；`full` 仍用于原来的完整站序流程。

- 正面：按红色“开往”文字的位置分开裁取首末站。分隔词不送 OCR，因此不会因“开往”读成“开住”而丢失走向。
- 反面：按红色分隔箭头定位站名行，去掉设施小图标及底部说明栏，只识别两端站名和需要确认的相邻括号附注。箭头向左时交换首末站；无法确认箭头时加入复核。
- 两面分别保留来源记录，不按正反面文件序号推断走向。文件名方位仍只用于 `station_key`。
- 文件名中的线路号用作提示，与 OCR 结果冲突或 OCR 不确定时进入复核；`route_ocr` 保存实际识别结果。`地铁接驳6号专线` 统一为 `接驳6`，`K1`、`通学专线13` 保留前缀。
- 端点坐标保留原图位置，人工修正和 VLM 复核继续使用原图裁片。清楚分隔的竖排字会重排识别；不能可靠分字的列保留竖排顺序。

未匹配已知布局的横图会明确报错，不从当前站或下站补造首末站。横版验证记录见 [横版验证](landscape-validation.md)。
