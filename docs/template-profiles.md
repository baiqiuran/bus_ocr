# 模板族阈值修改规则

本规则约束 `buspipe` 版面定位阈值的修改。目标不是让新模板“能跑”，而是让新模板得到独立分支，同时保证旧模板结果不回归。

## 强制顺序

### 1. 按模板族分派

新增阈值必须能回答“只对哪一族生效”。匹配条件至少使用以下一种，推荐组合使用：

- `width` / `height`
- `width / height` 比例
- 竖版 / 横版方向
- 模板 key，例如 `V_8505x19489`、`L_15910x2469`
- 文件名族特征（谨慎使用）

不能因为一张新图片失败，就修改 `LayoutSettings` 的全局默认值。

### 2. 优先使用比例

固定 px 只适合固定分辨率。新模板应优先使用宽高比例或相对边距。

例如：

```python
from dataclasses import replace

def relative_layout(layout, width, height):
    # 仅为示意：具体比例必须用基准图验证
    return replace(
        layout,
        stop_x0=round(0.07 * width),
        stop_x1=round(0.96 * width),
        row_project_x0=round(0.16 * width),
        row_project_x1=round(0.94 * width),
    )
```

比例值必须用目标族的整批样本验证，不能只验证修复样本。横版已有阈值大多使用比例；竖版新增分支也应优先使用比例。

### 3. 保留旧模板分支

旧模板继续使用原有 `PipelineSettings` / `LayoutSettings`，不做“顺手统一”或“顺便重构”。

新分支通过 `buspipe/templates.py` 的 `TemplateProfile` 注册：

```python
from dataclasses import replace
from buspipe.templates import TemplateProfile, PROFILES

new_family = TemplateProfile(
    name="wide_vertical_v1",
    version="1",
    min_width=8000,
    orientation="V",
    template_keys=("V_8505x19489",),
    layout_transform=lambda layout, width, height: replace(layout, stop_x1=8000),
)
```

`PROFILES` 目前注册了两个新版模板族：

| profile | version | 精确 key | 修改 |
| --- | --- | --- | --- |
| `vertical_8505_ratio` | 1 | `V_8505x19489`、`V_8504x19489` | route/stop/row 全部按宽度比例 |
| `vertical_4726_right` | 1 | `V_4726x7678` | 只把 `row_project_x1` 调到 `0.90W`；`stop_x1` 保持 4080 |

`V_4253x9745`、`V_2836x6497`、`V_2042x4678` 没有注册 profile，继续走 legacy 分支。

8505/8504 的当前比例：

```text
route_x0=0.07W   route_x1=0.22W   route_scan_x1=0.28W
stop_x0=0.08W    stop_x1=0.95W
row_x0=0.08W     row_x1=0.94W
route_min=0.03W  route_max=0.08W
```

4726 的当前比例：

```text
row_project_x1=0.90W
stop_x1 保持原 4080
```

`TemplateProfile` 的匹配字段：

| 字段 | 含义 |
| --- | --- |
| `min_width` / `max_width` | 像素宽范围 |
| `min_height` / `max_height` | 像素高范围 |
| `min_aspect` / `max_aspect` | 宽高比范围 |
| `orientation` | `V` 或 `L` |
| `template_keys` | 精确模板 key |
| `stem_contains` | 文件名片段 |
| `layout_transform` | 只替换 LayoutSettings |
| `landscape_transform` | 只替换 LandscapeSettings |

没有匹配 profile 时返回 `legacy`，设置对象和缓存 key 都不变。

### 4. 修改前固定基准

修改前先从当时的全量成功数据固定一份基准，至少包含：

- 每个模板族的模板 key / width / height
- 成功图片数
- 线路记录数
- 空线路号、未知方位、待复核数量
- 每个模板族的首末站样例

修改后使用**新的输出目录和 manifest** 重跑目标族，并与基准逐项比较。不能复用旧输出或旧缓存来声称“无回归”。

## 缓存与 profile

- 活跃 profile 的 `name:version` 会进入 poster/block 缓存 key。
- 修改 profile 的 `version` 会隔离缓存。
- 没有 profile 时保持旧 fingerprint，避免无意义地使历史缓存失效。
- 修改 `buspipe/landscape.py` 中未进入 settings 的模块常量时，profile 缓存 key 不会自动变化；必须提升 profile version 或使用 `--no-cache` / 新输出目录。

## 回归清单

```powershell
python -m pytest tests/test_template_profiles.py -q
python -m pytest tests/test_endpoints.py tests/test_landscape.py tests/test_route_geometry.py tests/test_frame_geometry.py tests/test_core_baseline.py -q
python -m pytest -q
```

除合成测试外，必须用旧模板真实样本做 A/B：

1. 旧输出 `station_routes.csv` 固定为 baseline；
2. 只选中一个模板族重跑；
3. 比较线路号、首末站、站点方位、空值、复核数；
4. 检查 `partial`、`failed`、失败原因是否增加；
5. 新模板族通过后，再决定是否注册到默认 `PROFILES`。

## 当前状态

- `buspipe/templates.py` 已提供 profile 匹配和缓存隔离接口。
- 已注册 `vertical_8505_ratio:1` 和 `vertical_4726_right:1`。
- `V_4253x9745`、`V_2836x6497`、`V_2042x4678` 保持 legacy。
- 8505/8504 本轮探测 19 张剩余失败，18 张能完成 OCR 并至少得到一对合理首末站；1 张仍无线路号板块。少数图片存在部分板块端点无效，但已不再整张失败。
- 4726 成功率样本的右端 `p95` 约 4078 px，仍在原 `stop_x1=4080` 内；原限制主要是 `row_project_x1=4000`，因此 profile 只扩 row，不扩 stop，避免把右侧广告/二维码列并入。
- 4726 抽样 10 张旧结果与新 profile 比较，新结果多次把截断的末站恢复为完整终点；73 张成功率模板的几何回归中 35 张 row 区间变化，仍需继续做全量 OCR A/B 后才能提升 version 或作为最终默认。
- 全量测试已通过；回归探测明细见 `output/full-20261023-endpoints/template_profile_regression_8505.csv`。
