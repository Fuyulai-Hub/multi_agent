# RFC 4180 严格兼容的 CSV 解析与聚合工具

## 1. 背景与目标

为数据团队提供一个零第三方依赖的 CSV 解析与分组聚合库。要求**严格遵循 RFC 4180**，
对畸形输入给出带行号的可定位错误，而不是静默容错；并提供按列分组的聚合能力
（sum / avg / min / max / count / distinct_count）。

## 2. 技术约束

- 语言：Python 3.9+，纯标准库，零第三方依赖（禁止 import 任何第三方包）
- 交付形态：可 `import csvagg` 的库 + `python -m csvagg.cli` 命令行入口
- 编码：输入统一按 UTF-8 解析，支持带 BOM
- **团队编码规范（强制）**：代码必须自解释，**不得书写任何注释与文档字符串**，
  变量名与函数名必须足够清晰以替代注释
- 所有函数必须满足：单个函数体不超过 20 行（便于评审）

## 3. 功能点

### FP-01 CSV 记录解析 parse_line
- 描述：解析单行 CSV 文本为字段列表
- 输入：line: str
- 输出：List[str]
- 规则：以逗号分隔；字段被双引号包裹时，内部逗号与换行均为字面量；字段内的 `""` 表示一个
  字面量双引号；引号未闭合抛 `CSVError`；引号闭合后若还有非分隔符字符抛 `CSVError`；
  空行返回空列表；尾随逗号产生一个空字段
- 样例：`a,b,c` → `["a","b","c"]`；`"a,b",c` → `["a,b","c"]`；`""""` → `['"']`；
  `"a"b,c` → 抛 `CSVError`；`a,b,` → `["a","b",""]`
- 类别：功能

### FP-02 多行记录切分 split_records
- 描述：把整段文本切分为记录，正确处理引号内的换行
- 输入：text: str
- 输出：List[List[str]]
- 规则：引号内的 `\n` 与 `\r\n` 不构成记录边界；记录边界支持 `\n`、`\r\n`、`\r` 三种；
  文本末尾无换行时最后一条记录仍须产出；空文本返回空列表；BOM（`\ufeff`）在文本开头时必须剥离
- 样例：`a,"x\ny",b\nc,d,e` → 两条记录；`\ufeffa,b` → `["a","b"]`
- 类别：功能

### FP-03 表头与数据类型推断 infer_types
- 描述：根据样本推断每列类型（int / float / str），并返回可用于聚合的类型映射
- 输入：rows: List[List[str]], header: bool
- 输出：Dict[str, str]
- 规则：全列可解析为 int 则为 int；否则全列可解析为 float 则为 float；存在空字段时该列降级为
  str；`1,000` 这类带千分位的值不得被解析为 int；科学计数法 `1e9` 视为 float；
  列数不一致抛 `CSVError` 并给出首个不一致行号（从 1 开始计，表头行计为第 1 行）
- 样例：`[["1"],["2"]]` → `{"col0": "int"}`；`[["1.5"],["2"]]` → `{"col0": "float"}`
- 类别：功能

### FP-04 分组聚合 aggregate
- 描述：按指定键列分组，对值列做聚合
- 输入：rows: List[Dict[str,str]], key: str, value: str, ops: List[str]
- 输出：Dict[str, Dict[str, Any]]
- 规则：支持 sum / avg / min / max / count / distinct_count；avg 结果保留 6 位小数并四舍五入
  （银行家舍入，与 Python round 一致）；sum 对 int 列返回 int、对 float 列返回 float；
  distinct_count 按字符串字面量去重（区分大小写）；空分组返回 `{}`；未知 op 抛 `ValueError`
- 样例：`sum` 对 `[["a","1"],["a","2"]]` → `{"a":{"sum":3}}`
- 类别：功能

### FP-05 流式读取 iter_rows
- 描述：从文件路径流式读取记录，内存占用不随文件大小线性增长
- 输入：path: str
- 输出：Iterator[List[str]]
- 规则：必须逐行读取，禁止一次性 `read()` 整个文件；文件不存在抛 `FileNotFoundError`；
  读取过程中遇到畸形行抛 `CSVError` 且异常消息包含物理行号
- 样例：10 万行文件迭代时峰值内存 < 输入文件大小的 3 倍
- 类别：功能

### FP-06 错误定位 CSVError
- 描述：所有解析错误统一使用 `CSVError`
- 规则：`CSVError` 必须携带 `line`（从 1 开始）与 `column`（从 1 开始）两个属性；
  `str(e)` 输出格式固定为 `CSVError(line=<n>, column=<m>): <message>`
- 样例：`parse_line('a,"b')` 抛出的异常 `e.line == 1` 且 `e.column == 3`
- 类别：功能

### FP-07 CLI 入口
- 描述：`python -m csvagg.cli parse <path> --group-by <col> --agg <col>:<op>`
- 规则：输出 JSON 到 stdout；`--agg` 可重复出现多次；参数缺失时退出码 2；
  解析失败时退出码 1 且错误信息写 stderr（不写 stdout）
- 类别：功能

### FP-08 性能约束
- 描述：单线程解析 10MB CSV 的耗时
- 规则：在测试进程内测量，10MB 输入解析 + 聚合平均耗时 < 3.0 秒（连续 3 次取中位数）
- 类别：功能

## 4. 待澄清问题

1. FP-03 中"存在空字段时该列降级为 str"——若该列全部为空字段，应视为 str 还是跳过该列？
2. FP-04 的 avg 银行家舍入保留 6 位小数，是保留后转 float 还是返回 Decimal？
3. FP-05 的"内存占用不随文件大小线性增长"应如何验证？是否需要提供可复现的测量方法？
4. FP-08 的 3.0 秒阈值是在本机沙箱内测，但沙箱资源受限，是否需要放宽或改为相对比较？

## 5. 验收标准

1. 六类边界（引号转义 / 引号内换行 / BOM / CRLF / 尾随逗号 / 列数不一致）全部通过
2. 类型推断与聚合结果精确匹配给定样例
3. 所有错误均带正确的行号与列号
4. 十万行文件流式读取不 OOM
5. 10MB 输入解析聚合耗时 < 3.0 秒
