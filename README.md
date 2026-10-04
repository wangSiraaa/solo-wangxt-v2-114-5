# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地两次调查（本演示为 **2019 → 2024**）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

---

## 1. 关键规则（对应验收要求）

### 1.1 胸径单位必须显式记录
* 每条测量必须带 `dbh_unit`（`cm`/`mm`/`in`），树高单位 `m`。
* 入库时转换为规范单位（胸径 cm、树高 m），**原始值与单位同时保留**，可审计。
* 合理范围检查拦截单位错误（如把 250 mm 当成 250 cm、树高 950 m）。
* 树干坐标必须落在样地边界内（PostGIS 层有 `ST_Contains` 约束兜底）。

### 1.2 异速生长方程显式记录适用树种
`AGB_kg = a · dbh_cm^b · h_m^c`，方程记录：
* 适用树种列表（多对多）、胸径适用范围、是否需要树高；
* 系数 a/b/c、残差 σ(ln AGB)、文献引用、版本号；
* 超出适用径阶的树会在结果中标记 **extrapolation**。

### 1.3 编号是标签，不是身份
**编号相同但位置矛盾时，先核实，不能直接认成同株。**

* 内部 `tree_id` 才是个体身份；同树行换标签 = 已核实改号（renumber）。
* 同编号、不同树行、位置矛盾 → 生成 `IdentityConflict`（open），
  在人工核实前**从所有分量中剔除**，不会悄悄变成死亡或进界。
* 核实结论只有人工给出：
  * `renumber`：同一株树换了牌号 → 计入存活木生长；
  * `distinct`：不同个体 → t1 计入死亡、t2 计入进界。
* 新编号出现在旧树附近 → “possible renumber” 待核实，绝不自动合并。

### 1.4 真实零生长、缺测、死亡三者严格区分
| 情况 | 字段 | 处理 |
|---|---|---|
| 真实零生长 | `alive_measured`，两次均测，|Δdbh| ≤ 0.15 cm 且有复核记录 | 计入生长量（增量≈0），结果中列出 |
| 缺测 | `alive_not_measured`（活着但胸径未测） | **不是零**；按样地比率插补，方差膨胀，列出清单 |
| 死亡 | `dead`（t2 有死亡观测） | 以 t1 生物量计入死亡量 |
| 未找到 | `missing_tree` | 不入死亡量，列入 provenance |
| 进界以下 | t2 新树 dbh < 5 cm | 记录但不计入进界 |

### 1.5 总体估计按抽样设计加权
分层简单随机抽样，**不把所有树木平均后乘面积**：

```
样地分量 y [kg/ha] = 分量(kg) / 该样地自己的面积(ha)
Y_h = A_h · mean_h(y)                 # A_h = 已知的层土地面积
SE_h = A_h · sqrt( (1−f_h) · s_h²/n )  # 有限总体校正可选
Y = Σ_h Y_h，SE 跨层合成（Welch–Satterthwaite 自由度，t 分布 95% CI）
```

演示数据刻意使用**不等面积样地**（0.20 / 0.50 / 1.00 ha）。

### 1.6 已确认调查版不可被新方程静默改变
* `EstimateVersion`：draft → `confirm` 后结果载荷、设计快照、方程校验和全部冻结。
* 模型层 + PostGIS 触发器双重禁止修改 confirmed 版本。
* 确认时同时**锁定所用方程**（系数不可改）；新系数必须以**新方程 code/version** 录入，
  并产生**新版本估计**，旧版本数字永不改变。

### 1.7 新版异速生长方程采用评审（差异只能来自方程本身）
研究团队采用新版方程前，必须先证明与既有已确认估计的差异**不**来自数据、身份判断或
抽样框漂移。主场景“方程采用评审”：

* 候选按 **candidate → validated → approved / withdrawn** 流转；候选以**不可变 JSON
  规格**保存适用树种、径阶（dbh 范围）、系数 a/b/c、残差 σ、文献、方程形式与校验结果。
  候选**不是** `AllometricEquation` 行，因此普通两期估计流程永远选不到它；已锁定方程
  也绝不被改动。
* **影响比较必须锁定一个既有 confirmed `EstimateVersion`**：重新读取该版的调查数据、
  人工身份决定（冲突核实/改号）与抽样设计快照，对测量、身份、样地/层/面积分别取 SHA-256
  指纹，并用**基准方程重算**——只有与已确认结果载荷逐字节一致（无数据/身份/框漂移）才
  接受比较；否则比较被拒绝（HTTP 409，记录审计事件）。
* 锁通过后，候选在**完全相同**的锁定输入上重算，输出：
  * **覆盖矩阵**（树种 × 径阶：覆盖/缺树种/超径阶计数与树号、外推清单、缺失覆盖）；
  * **逐树、逐样地、总体**三层基准版 vs 新版差异（生物量 kg/Mg、Δ、Δ%、SE）与外推。
* 比较状态 `complete` / `incomplete`：**缺少树种或超出候选径阶即 incomplete，禁止批准**。
* 批准时再次校验全部指纹与结果哈希未变，随后创建**全新的方程 code/version 行**与一个
  **独立的新 confirmed `EstimateVersion`**；基准版、基准方程、基准结果均不被修改。
  `EstimateVersion.generated_by_review` 为一对一关系——**同一评审并发批准只产生一个新版本**
  （行锁 + 唯一约束，数据库层保证；败者返回 409 与已生成版本号）。
* **候选被撤回（withdrawn）后，既有比较与全部事件作为审计保留，但不能再批准/确认。**
* 普通两期估计流程在不选择候选时行为完全不变（候选根本不出现在方程列表中）。


---

## 2. 不确定性假设（结果中完整输出）
1. **设计推断**：分层 SRS，每样地等权（每公顷基准），层土地面积放大；树木从不合并平均。
2. **测量误差**：胸径 σ=0.10 cm、树高 σ=0.30 m，独立高斯，一阶误差传播；
   作为诊断分量单独报告（不与样地间抽样方差重复计入 SE 合计）。
3. **方程残差**：乘法对数正态 σ(ln AGB)；存活木两次用同一方程、残差假定完全相关故增量抵消；
   死亡（仅 t1）与进界（仅 t2）的方程误差保留。
4. **缺测存活木**：假定样地内 MAR，用“有测样木 t1 生物量生长率”做比率插补，
   抽样方差按 1/(1−缺测生物量比例) 膨胀。
5. 未核实身份、未找到、进界以下个体**排除在分量外**并在 provenance 列明。
6. 95% CI 用跨层 Welch–Satterthwaite df 的 t 分布；净变化 SE 中分量抽样协方差假定为 0。
7. 样地申报面积与多边形面积交叉核对（1% 容差）。

---

## 3. 运行

### 后端
```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate      # 可选
pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo        # 载入虚构数据（含全部验收场景）
python3 manage.py runserver 127.0.0.1:8123
```

PostgreSQL/PostGIS：
```bash
FOREST_DB=postgis PGHOST=.. PGUSER=.. PGPASSWORD=.. \
  python3 manage.py migrate
psql -d foreststation -f ../deploy/postgis.sql
```

### 前端
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, /api 代理到 8123
```

界面四页：
1. **Plots & individuals**：SVG 地图显示全部样地边界与 t2 个体状态；点入样地看 t1→t2 复测、
   改号、零生长/缺测/死亡着色；
2. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct）；
3. **Estimates**：选择方程→跑 draft→查看分量、来源、不确定性→确认冻结；
4. **Equation reviews**：候选创建（系数/径阶/文献表）→校验→锁定 confirmed 基准做影响比较，
   查看**覆盖矩阵、差异来源（逐树/逐样地/总体）、外推与缺失覆盖**，完整方可批准生成独立新版本，
   或撤回；含历史评审与审批事件时间线。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2024` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open` | 同号位置矛盾 |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber|distinct, note}` |
| POST | `/api/imports/` | 批量入库（拒收单位错误/越界行，207 返回明细） |
| POST | `/api/estimates/` | 运行 draft 估计 |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |
| POST | `/api/equation-reviews/` | 创建候选（candidate_spec：树种→系数/径阶/文献） |
| POST | `/api/equation-reviews/{id}/validate/` | 结构/域校验 → validated |
| POST | `/api/equation-reviews/{id}/compare/` | 锁定 confirmed 基准版并做影响比较/覆盖矩阵 |
| POST | `/api/equation-reviews/{id}/approve/` | 仅 complete 可比：生成新方程行 + 新 confirmed 版本 |
| POST | `/api/equation-reviews/{id}/withdraw/` | 撤回（比较保留审计，禁止批准） |
| GET | `/api/equation-reviews/` / `{id}/` | 评审列表/详情（内嵌审批与事件历史） |
| GET | `/api/equation-reviews/{id}/comparisons/` | 覆盖矩阵、逐树/逐样地/总体差异、锁定指纹 |

### 候选规格示例
```json
{
  "label": "2026 station re-fit",
  "candidate_spec": {
    "OAK": {"code": "OAK-AGB", "version": "2026-review",
            "a": 0.128, "b": 2.395, "c": 0.602,
            "dbh_min_cm": 5.0, "dbh_max_cm": 120.0,
            "height_required": true, "residual_sigma": 0.18,
            "citation": "fictional oak 2026 refit"}
  }
}
```
比较请求：`POST /api/equation-reviews/{id}/compare/`，body `{"baseline_version": 7}`。
完整覆盖返回 201（`status: complete`）；缺树种/超径阶返回 202（`status: incomplete`）；
基准不可复现（漂移）返回 409。


### 入库行示例
```json
{
  "campaign": "2024",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
21 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及**方程采用评审**：

* 完整覆盖的新方程批准后生成**独立新版本**、旧版本数字与基准方程不变；
* **缺少树种**或**超出径阶**时比较标记 `incomplete` 且批准被禁止（409）；
* 基准版因测量/身份/框变更而**不可复现**时比较被拒绝（409），不留伪比较；
* 候选**撤回**后比较与事件审计保留，但不能再批准；
* **同一评审并发/重复批准只产生一个新版本**（唯一约束 + 行锁；重复批准 409）；
* 普通两期估计流程不选择候选时行为完全不变（候选不进入方程列表）。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等；
* 两个**方程采用评审候选**：`2026 station-wide re-fit`（三树种全覆盖、径阶放宽到可覆盖
  P04/002 的 102 cm，可批准）与 `2026 oak-only … capped at 90 cm`（仅 OAK 且 90 cm 上限，
  比较必为 **incomplete**：P04/002 超径阶）。先在 Estimates 页确认一版基准，再到
  Equation reviews 页 validate → compare → approve/withdraw。
