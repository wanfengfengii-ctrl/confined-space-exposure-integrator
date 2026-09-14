# 受限空间气体八小时等效暴露裁决 API

纯后端裁决服务：接收一条覆盖八小时（0–28800 秒）的气体浓度采样序列，使用**十进制定点
数**对相邻采样点做梯形积分，得到时间加权等效值并对 25.000 ppm 阈值作出 PASS/FAIL
裁决。采样间隔无需均匀——长时间高浓度区段会按其实际时长正确加权，而不会像
"按采样条数求平均"那样被低估。

- 运行时：Python 3.12 · FastAPI · Pydantic v2 · pytest
- 全部浓度计算使用 `decimal.Decimal`，等效值按 `ROUND_HALF_UP` 保留三位小数

## 数学定义

对相邻采样点 `(tᵢ, pᵢ)`、`(tᵢ₊₁, pᵢ₊₁)` 逐段累加梯形面积：

```
area       = Σ (pᵢ + pᵢ₊₁) × (tᵢ₊₁ − tᵢ) ÷ 2        （单位：ppm·秒，精确十进制）
equivalent = ROUND_HALF_UP(area ÷ 28800, 3 位小数)
verdict    = PASS  若 equivalent ≤ 25.000，否则 FAIL
```

## API 契约

### `POST /adjudicate`

请求体为且仅为一条 JSON 采样序列（数组），且请求必须以 JSON 媒体类型提交
（`application/json`，可带 `; charset=...` 等参数）；`text/plain`、未声明
`Content-Type` 或其他非 JSON 媒体类型的请求按 `invalid_type` 整批拒绝。
每个元素：

| 字段        | 类型       | 约束                                             |
| ----------- | ---------- | ------------------------------------------------ |
| `timestamp` | 整数（秒） | 首点必须为 `0`，末点必须为 `28800`，全程严格递增 |
| `ppm`       | JSON 数字  | `0 ≤ ppm ≤ 1000`，最多三位小数                   |

`ppm` 必须是 JSON 数字；字符串形式（即使内容合法，如 `"12.340"`）会按
`invalid_type` 整批拒绝。

查询参数（均为可选）：

| 参数                 | 类型    | 默认   | 说明                                                                                                                                 |
| -------------------- | ------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `include_exceedance` | 布尔量  | `false` | 为真时，在完成原有整批校验与裁决**之后**，把相邻采样点之间按线性变化处理，附加严格高于 `25.000` ppm 的超限分析（见下）。取值遵循 FastAPI 布尔解析：`true`/`1`/`yes`/`on`（大小写不敏感）为真，`false`/`0`/`no`/`off` 为假；无法解析时返回 `422`，错误定位于查询参数 `include_exceedance`。 |

#### 合法响应 `200`

省略 `include_exceedance`（或显式给假值）时，响应结构与既有版本完全一致：

```json
{
  "area": "1435000",
  "equivalent": "49.826",
  "verdict": "FAIL"
}
```

- `area`：原始积分面积（未舍入的精确十进制，序列化为字符串以保留精确表示）
- `equivalent`：三位小数的八小时等效值（字符串，如 `"25.000"`）
- `verdict`：唯一裁决，`"PASS"` 或 `"FAIL"`

##### `include_exceedance=true` 时附加的 `exceedance`

相邻采样点之间视为线性浓度曲线，用 `Decimal` 求解浓度穿越 `25.000` ppm 的
精确秒点（穿越秒点按 `ROUND_HALF_UP` 保留至多三位小数，即毫秒精度）：

```
t_cross = t_i + (25.000 − ppm_i) ÷ (ppm_i+1 − ppm_i) × (t_i+1 − t_i)
```

**舍入只发生在输出边界**：穿越秒点、区段时长与总时长在内部一律保留高精度
Decimal 精确值，仅在序列化时对每个输出值单独 `ROUND_HALF_UP` 到三位小数。
绝不会先舍入两个穿越点再相减——否则每个短区段都会被双向放大至多约 0.001 秒
（例如三次瞬时越线各真实持续 4/3 秒时，总计会错误显示为 `4.002` 而非 `4`）。

仅统计**严格高于**阈值的时间；恰好等于阈值的采样点或区段不计超限。多个在
（严格高于阈值的）采样点处相接的区段合并为一条最大连续区段；若最长持续时间
并列，返回开始秒数最早的区段。

```json
{
  "area": "576100",
  "equivalent": "20.003",
  "verdict": "PASS",
  "exceedance": {
    "total_seconds": "10",
    "longest_segment": {
      "start": "5",
      "end": "15",
      "duration_seconds": "10"
    }
  }
}
```

- `total_seconds`：严格高于阈值的累计秒数（字符串，至多三位小数；无超限时为 `"0"`）
- `longest_segment.start` / `end`：最长连续超限区段的起止秒（字符串，至多三位小数）
- `longest_segment.duration_seconds`：该区段持续秒数（字符串，至多三位小数）
- 全程未严格超过阈值（例如全程恰为 `25.000`，或只在峰值瞬时触及 `25.000`）时：
  `total_seconds` 为 `"0"`、`longest_segment` 为 `null`

> 超限分析是纯粹的附加结果：任何采样无效情形仍只返回既有"唯一首错"信封，
> 且分析阶段在面积/裁决计算之后才运行，异常时也不会泄漏面积或裁决。

#### 校验失败响应 `422`

整批拒绝，返回唯一首错信封，**绝不包含** `area` / `equivalent` / `verdict`：

```json
{
  "error": {
    "index": 2,
    "category": "non_monotonic_timestamp",
    "message": "timestamp 100 is not strictly greater than previous timestamp 100"
  }
}
```

错误类别（`category`）：

| category                  | 含义                                                       |
| ------------------------- | ---------------------------------------------------------- |
| `invalid_type`            | 请求体或采样点结构/类型不合法（非 JSON 媒体类型、非数组、非对象、缺/多/重复字段、非整数时间戳、ppm 非 JSON 数字或为非有限值、JSON 无法解析） |
| `missing_endpoint`        | 首点不是 0 或末点不是 28800                                |
| `non_monotonic_timestamp` | 时间戳重复或逆序（未严格递增）                             |
| `ppm_out_of_range`        | ppm 超出 `[0, 1000]`                                       |
| `ppm_precision_exceeded`  | ppm 小数位超过三位                                         |

**首错定位规则**：多个错误并存时（含类型错误与领域错误混合），先按采样点索引升序
定位；同一索引存在多类错误时，依次按 类型/结构 → 端点 → 时间递增 → ppm 范围 →
ppm 精度 的优先级选择唯一首错。任何 422 响应都只有这一个错误信封，不会返回
框架级的多项明细。

**精度按字面形式判定**：服务端以 `Decimal` 直接解析原始 JSON 数字，尾随零不会丢
失——`12.3400` 虽数值等于 `12.34`，仍因四位小数被整批拒绝；`12.340` 则合法。

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

## 示例

```bash
# 不均匀采样：200 秒处冲到 100 ppm 并保持到 28600 秒之后回落，
# 按条数平均只有 25 ppm，时间加权等效值却是 49.826 ppm → FAIL
curl -X POST http://localhost:8000/adjudicate \
  -H 'Content-Type: application/json' \
  -d '[{"timestamp":0,"ppm":0},{"timestamp":100,"ppm":0},
       {"timestamp":200,"ppm":100},{"timestamp":28800,"ppm":0}]'
# => {"area":"1435000","equivalent":"49.826","verdict":"FAIL"}

# 阈值边界：等效值恰为 25.0005，ROUND_HALF_UP 得 25.001 → FAIL
curl -X POST http://localhost:8000/adjudicate \
  -H 'Content-Type: application/json' \
  -d '[{"timestamp":0,"ppm":25},{"timestamp":28800,"ppm":25.001}]'
# => {"area":"720014.400","equivalent":"25.001","verdict":"FAIL"}

# 末点缺失 → 422
curl -X POST http://localhost:8000/adjudicate \
  -H 'Content-Type: application/json' \
  -d '[{"timestamp":0,"ppm":1},{"timestamp":100,"ppm":1}]'
# => 422 {"error":{"index":1,"category":"missing_endpoint","message":"..."}}

# 瞬时越线判定：20→30→20 的小尖峰，插值得到第 5 秒上穿、第 15 秒下穿，
# 严格高于 25.000 ppm 共 10 秒
curl -X POST 'http://localhost:8000/adjudicate?include_exceedance=true' \
  -H 'Content-Type: application/json' \
  -d '[{"timestamp":0,"ppm":20},{"timestamp":10,"ppm":30},
       {"timestamp":20,"ppm":20},{"timestamp":28800,"ppm":20}]'
# => {"area":"576100","equivalent":"20.003","verdict":"PASS",
#     "exceedance":{"total_seconds":"10",
#       "longest_segment":{"start":"5","end":"15","duration_seconds":"10"}}}

# 查询参数无法解析为布尔值 → 422，定位到该查询参数（采样体不参与处理）
curl -X POST 'http://localhost:8000/adjudicate?include_exceedance=maybe' \
  -H 'Content-Type: application/json' -d '[]'
# => 422 {"detail":[{"type":"bool_parsing",
#       "loc":["query","include_exceedance"], ...}]}

# 四位尾随小数 → 422（精度按字面形式判定）
curl -X POST http://localhost:8000/adjudicate \
  -H 'Content-Type: application/json' \
  -d '[{"timestamp":0,"ppm":12.3400},{"timestamp":28800,"ppm":12.3400}]'
# => 422 {"error":{"index":0,"category":"ppm_precision_exceeded","message":"..."}}
```

## 本地运行

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

交互式文档位于 `http://localhost:8000/docs`。

## 测试

```bash
pytest -q
```

## Docker

```bash
docker build -t gas-adjudication-api .
docker run --rm -p 8000:8000 gas-adjudication-api
```

## Docker Compose

```bash
# 启动 API（宿主端口默认 8000，可用 API_PORT 覆盖）
docker compose up api
API_PORT=9000 docker compose up api

# 运行一次性测试服务 verify（跑完 pytest 即退出）
docker compose run --rm verify
# 或让退出码反映测试结果：
docker compose up --exit-code-from verify verify
```

## 项目结构

```
app/
  main.py     # FastAPI 应用、路由、422 错误信封
  parsing.py  # 原始 JSON 解码（保留字面精度）与单趟交错校验
  core.py     # 领域校验、梯形积分、等效值与裁决（纯 Decimal）
  models.py   # Pydantic 请求/响应模型
tests/
  test_core.py     # 积分、舍入、阈值与错误优先级单元测试
  test_parsing.py  # 解码与交错校验单元测试
  test_api.py      # HTTP 层契约测试
Dockerfile
docker-compose.yml   # api 服务 + 一次性 verify 测试服务
requirements.txt
```

## 设计说明

- **十进制定点数**：所有积分与舍入均使用 `decimal.Decimal`，避免二进制浮点误差；
  面积最多四位小数，除以 28800 后按 `ROUND_HALF_UP` 精确落到三位小数。
- **字面精度保留**：请求体由服务端以 `json.loads(parse_float=Decimal)` 自行解码，
  JSON 数字不经过二进制浮点，`12.3400` 的四位小数对精度规则可见。
- **重复字段即结构不明**：解码时通过 `object_pairs_hook` 记录重复出现的字段名，
  在按索引升序的同一趟校验中于正确索引处整批拒绝，绝不静默采用最后值。
- **单趟交错校验**：类型/结构校验与领域校验在同一趟索引升序扫描中交错进行，
  因此任何 422 都是按索引定位的唯一首错，而非框架默认的多项明细。
- **精确序列化**：响应中的 `area` 与 `equivalent` 以字符串返回，保留精确十进制
  表示（含末尾零，如 `"25.000"`），调用方可无损解析。
- **先校验后计算**：任何校验失败都在积分之前整批拒绝，错误信封因此不可能泄漏
  面积、等效值或裁决。
- **超限分析按需附加**：仅当 `include_exceedance=true` 时，才在既有校验与裁决
  完成后做穿越分析；相邻采样点按线性变化处理，穿越秒点与持续时长全部使用
  `Decimal`（秒值至多三位小数）。该分析不参与 PASS/FAIL 裁决，任何异常也不会
  回泄面积或裁决；省略参数时响应与当前版本逐字段一致。
