# Similar case / Test case 数据切分设计

## 1. 目标与口径

对 `data_output/first_time_seq1_dataset_icd_selected_with_discharge.csv` 做一次互斥、可复现的分层切分：

- `similar case` 约占 80%，`test case` 约占 20%；
- 以规范化后 `icd_code` 的前三位作为疾病组（下文记为 `icd3`）；
- 对整数样本量允许的每个 `icd3` 组，test 占比必须位于 **15%–25%**，即相对 20% 目标最多偏差 5 个百分点；similar 占比相应位于 75%–85%；
- 同一条记录只能出现在一个集合中，两个集合并集必须等于原数据集；
- 切分结果不依赖原 CSV 行顺序，并能通过固定规则完全复现。
- `mimic_similar.csv` 不保留原始 `text` 字段，而是将其中指定的出院小结段落结构化为独立字段；
- `mimic_test.csv` 不保留原始 `text` 字段，而是保留该字段中 `Discharge Medications:` 标题之前的原文。

这里的“偏差 5%”按 **5 个百分点**解释。例如 test 占比 15% 或 25% 均合格。

## 2. 当前数据基线

本设计基于 2026-07-29 20:51:45 +0800 的源文件：

- 文件大小：72,116,486 bytes；
- SHA-256：`c887c2c96b3c2416f1f512bcbf8f39cff34524b283a1bb640847808aa21229b4`；
- 字段：`subject_id`、`hadm_id`、`admittime`、`seq_num`、`icd_code`、`icd_version`、`text`；
- 记录数：6,777；
- `icd3` 组数：109；完整 ICD 编码数：479；
- `icd_version` 全部为 10，`seq_num` 全部为 1；
- `subject_id` 和 `hadm_id` 均无重复；
- `icd_code` 与 `text` 均无空值，未发现完全重复的 `text`；
- 当前编码没有小数点、大小写或首尾空白问题，但实现仍使用统一规范化规则。

整体目标数量取最接近总数 20% 的整数：

```text
test_target = floor(6777 × 0.20 + 0.5) = 1355
similar_target = 6777 - 1355 = 5422
```

因此预期整体结果为：

| 集合         | 记录数 |      占比 |
| ------------ | -----: | --------: |
| similar case |  5,422 |  80.0059% |
| test case    |  1,355 |  19.9941% |
| 合计         |  6,777 | 100.0000% |

## 3. 关键限制：极小疾病组无法严格满足 ±5 个百分点

若某个疾病组有 `n` 条记录、其中 `k` 条进入 test，则严格条件为：

```text
0.15 ≤ k / n ≤ 0.25
```

由于 `k` 必须是整数，当前有 27 个疾病组不存在满足该区间的整数解，共涉及 54 条记录：

| 组大小 | 组数 | 疾病组                                                          | 最接近 80:20 的分配（similar:test） |
| -----: | ---: | --------------------------------------------------------------- | ----------------------------------: |
|      1 |   13 | A54、A56、B00、C26、C44、C48、D18、E16、E80、K12、K27、N82、O62 |                                 1:0 |
|      2 |    9 | A05、A18、B18、C88、D00、D3A、K60、K68、S36                     |                                 2:0 |
|      3 |    3 | A03、D37、R16                                                   |                                 2:1 |
|      7 |    2 | K30、Q44                                                        |                                 6:1 |

例如，`n = 7` 时 1 条 test 为 14.29%，2 条 test 为 28.57%，两者都超出 15%–25%；选 1 条是离 20% 更近的方案。

因此本方案采用以下优先级：

1. 整体严格取最接近 80:20 的 5,422:1,355；
2. 对有整数可行解的 82 个 `icd3` 组，严格保证 test 为 15%–25%；
3. 对上述 27 个不可行小组，选择与 20% 绝对偏差最小的整数 test 数；若未来出现等距情况，优先较小的 test 数，使更多罕见病例保留在 similar 集合；
4. 通过其他可行组在允许区间内的微调，补足整体 test 目标数。

按此规则，不可行小组共分为 49 条 similar、5 条 test；其余 82 个可行组共分为 5,373 条 similar、1,350 条 test，后者 test 占比为 20.0803%。如果要求 109 个组无一例外全部落入 15%–25%，则当前数据不存在二分方案，只能先删除、合并极小疾病组，或放宽容差。

## 4. 分层配额算法

### 4.1 ICD 分组

仅将规范化用于比较和分组，不修改输出中的原始编码：

```text
normalize_icd(value) = upper(trim(string(value))).replace(".", "")
icd3 = normalize_icd(icd_code)[:3]
```

规范化后编码少于 3 个字符、为空，或不符合 `^[A-Z][A-Z0-9]{2,6}$` 时停止切分并报告，不静默修复。

### 4.2 每组上下界

对每个组 `g`，设样本数为 `n_g`、test 配额为 `k_g`：

```text
L_g = ceil(0.15 × n_g)
U_g = floor(0.25 × n_g)
```

- 若 `L_g ≤ U_g`，要求 `L_g ≤ k_g ≤ U_g`；
- 若 `L_g > U_g`，将该组标记为 `integer_infeasible`，并固定为使 `|k_g / n_g - 0.20|` 最小的整数配额。

### 4.3 在满足整体目标的前提下确定配额

1. 可行组从 `k_g = L_g` 开始，不可行组使用上节的固定配额；
2. 先验证整体目标 1,355 位于所有组配额之和的可达范围内，否则停止并报告；
3. 在未达到 1,355 时，每次向仍未到 `U_g` 的组增加 1 个 test 名额；
4. 每次选择增加后对分组比例平方误差增量最小的组：

```text
delta(g) = ((k_g + 1) / n_g - 0.20)^2 - (k_g / n_g - 0.20)^2
```

5. `delta(g)` 相同时按 `icd3` 字典序选择，保证结果确定；
6. 最终校验 `sum(k_g) = 1355`。

该方法在配额上下界内最小化各疾病组 test 比例相对 20% 的平方偏差，同时保证整体数量精确命中目标。

## 5. 组内记录选择

配额确定后，不使用 CSV 当前顺序直接截取。为每条记录生成稳定散列分数：

```text
split_key = subject_id + "|" + hadm_id
score = SHA256("mimic2-similar-test-v1|" + split_key)
```

在每个 `icd3` 内按 `score` 升序排列；若散列分数相同，再按 `subject_id`、`hadm_id` 升序排列。前 `k_g` 条进入 test，其余进入 similar。

这样可以：

- 消除原文件按患者、入院时间或编码排序造成的系统性偏差；
- 使用固定版本字符串实现完全复现；
- 避免伪随机数生成器和行顺序改变结果；
- 用 `subject_id + hadm_id` 作为记录身份，便于清单审计。

当前每个 `subject_id` 和 `hadm_id` 都只出现一次，因此不存在同一患者跨集合泄漏。实现时仍应先检查唯一性；若未来同一 `subject_id` 有多条记录，应改为以患者为不可拆分单元重新计算配额，而不能把同一患者分到两个集合。

## 6. 当前文件的精确配额基线

星号表示因整数限制无法进入 15%–25% 区间的组；其他 82 个组均满足要求。若源文件 SHA-256 变化，应重新运行配额算法，不能继续硬编码本表。

| ICD3           |            总数 |         Similar |            Test |          Test 占比 |      例外      |
| -------------- | --------------: | --------------: | --------------: | -----------------: | :-------------: |
| A02            |               4 |               3 |               1 |             25.00% |                |
| A03            |               3 |               2 |               1 |             33.33% |        *        |
| A04            |              82 |              66 |              16 |             19.51% |                |
| A05            |               2 |               2 |               0 |              0.00% |        *        |
| A08            |              86 |              69 |              17 |             19.77% |                |
| A09            |              61 |              49 |              12 |             19.67% |                |
| A18            |               2 |               2 |               0 |              0.00% |        *        |
| A54            |               1 |               1 |               0 |              0.00% |        *        |
| A56            |               1 |               1 |               0 |              0.00% |        *        |
| B00            |               1 |               1 |               0 |              0.00% |        *        |
| B15            |              22 |              18 |               4 |             18.18% |                |
| B16            |              10 |               8 |               2 |             20.00% |                |
| B17            |              40 |              32 |               8 |             20.00% |                |
| B18            |               2 |               2 |               0 |              0.00% |        *        |
| B19            |              10 |               8 |               2 |             20.00% |                |
| B37            |               8 |               6 |               2 |             25.00% |                |
| C15            |              62 |              50 |              12 |             19.35% |                |
| C16            |              83 |              66 |              17 |             20.48% |                |
| C17            |              13 |              10 |               3 |             23.08% |                |
| C18            |             154 |             123 |              31 |             20.13% |                |
| C19            |              29 |              23 |               6 |             20.69% |                |
| C20            |              58 |              46 |              12 |             20.69% |                |
| C21            |              10 |               8 |               2 |             20.00% |                |
| C22            |             109 |              87 |              22 |             20.18% |                |
| C23            |              16 |              13 |               3 |             18.75% |                |
| C24            |              32 |              26 |               6 |             18.75% |                |
| C25            |             207 |             165 |              42 |             20.29% |                |
| C26            |               1 |               1 |               0 |              0.00% |        *        |
| C44            |               1 |               1 |               0 |              0.00% |        *        |
| C48            |               1 |               1 |               0 |              0.00% |        *        |
| C49            |              21 |              17 |               4 |             19.05% |                |
| C78            |              83 |              66 |              17 |             20.48% |                |
| C7A            |              43 |              34 |               9 |             20.93% |                |
| C7B            |              11 |               9 |               2 |             18.18% |                |
| C88            |               2 |               2 |               0 |              0.00% |        *        |
| D00            |               2 |               2 |               0 |              0.00% |        *        |
| D12            |              54 |              43 |              11 |             20.37% |                |
| D13            |              46 |              37 |               9 |             19.57% |                |
| D18            |               1 |               1 |               0 |              0.00% |        *        |
| D37            |               3 |               2 |               1 |             33.33% |        *        |
| D3A            |               2 |               2 |               0 |              0.00% |        *        |
| E16            |               1 |               1 |               0 |              0.00% |        *        |
| E80            |               1 |               1 |               0 |              0.00% |        *        |
| I81            |              36 |              29 |               7 |             19.44% |                |
| I82            |               4 |               3 |               1 |             25.00% |                |
| I85            |               4 |               3 |               1 |             25.00% |                |
| I86            |              10 |               8 |               2 |             20.00% |                |
| K12            |               1 |               1 |               0 |              0.00% |        *        |
| K20            |              12 |              10 |               2 |             16.67% |                |
| K21            |              56 |              45 |              11 |             19.64% |                |
| K22            |              81 |              65 |              16 |             19.75% |                |
| K25            |             115 |              92 |              23 |             20.00% |                |
| K26            |              89 |              71 |              18 |             20.22% |                |
| K27            |               1 |               1 |               0 |              0.00% |        *        |
| K28            |              29 |              23 |               6 |             20.69% |                |
| K29            |              51 |              41 |              10 |             19.61% |                |
| K30            |               7 |               6 |               1 |             14.29% |        *        |
| K31            |              71 |              57 |              14 |             19.72% |                |
| K35            |             577 |             460 |             117 |             20.28% |                |
| K36            |               6 |               5 |               1 |             16.67% |                |
| K37            |              12 |              10 |               2 |             16.67% |                |
| K40            |              10 |               8 |               2 |             20.00% |                |
| K41            |               6 |               5 |               1 |             16.67% |                |
| K42            |              45 |              36 |               9 |             20.00% |                |
| K43            |              86 |              69 |              17 |             19.77% |                |
| K44            |              67 |              54 |              13 |             19.40% |                |
| K45            |               5 |               4 |               1 |             20.00% |                |
| K46            |              13 |              10 |               3 |             23.08% |                |
| K50            |             182 |             145 |              37 |             20.33% |                |
| K51            |             157 |             126 |              31 |             19.75% |                |
| K52            |             122 |              98 |              24 |             19.67% |                |
| K55            |              84 |              67 |              17 |             20.24% |                |
| K56            |             265 |             212 |              53 |             20.00% |                |
| K57            |             449 |             359 |              90 |             20.04% |                |
| K58            |              13 |              10 |               3 |             23.08% |                |
| K59            |              47 |              38 |               9 |             19.15% |                |
| K60            |               2 |               2 |               0 |              0.00% |        *        |
| K61            |              45 |              36 |               9 |             20.00% |                |
| K62            |              61 |              49 |              12 |             19.67% |                |
| K63            |              44 |              35 |               9 |             20.45% |                |
| K64            |              34 |              27 |               7 |             20.59% |                |
| K65            |              38 |              30 |               8 |             21.05% |                |
| K66            |              37 |              30 |               7 |             18.92% |                |
| K68            |               2 |               2 |               0 |              0.00% |        *        |
| K70            |             348 |             278 |              70 |             20.11% |                |
| K71            |              38 |              30 |               8 |             21.05% |                |
| K72            |              94 |              75 |              19 |             20.21% |                |
| K74            |              93 |              74 |              19 |             20.43% |                |
| K75            |              51 |              41 |              10 |             19.61% |                |
| K76            |              25 |              20 |               5 |             20.00% |                |
| K80            |             835 |             666 |             169 |             20.24% |                |
| K81            |              88 |              70 |              18 |             20.45% |                |
| K82            |               5 |               4 |               1 |             20.00% |                |
| K83            |             222 |             177 |              45 |             20.27% |                |
| K85            |             370 |             296 |              74 |             20.00% |                |
| K86            |              56 |              45 |              11 |             19.64% |                |
| K90            |               5 |               4 |               1 |             20.00% |                |
| K91            |              65 |              52 |              13 |             20.00% |                |
| K92            |              77 |              62 |              15 |             19.48% |                |
| N82            |               1 |               1 |               0 |              0.00% |        *        |
| O62            |               1 |               1 |               0 |              0.00% |        *        |
| Q27            |               5 |               4 |               1 |             20.00% |                |
| Q43            |               6 |               5 |               1 |             16.67% |                |
| Q44            |               7 |               6 |               1 |             14.29% |        *        |
| R16            |               3 |               2 |               1 |             33.33% |        *        |
| R18            |               8 |               6 |               2 |             25.00% |                |
| S36            |               2 |               2 |               0 |              0.00% |        *        |
| T85            |               6 |               5 |               1 |             16.67% |                |
| T86            |               9 |               7 |               2 |             22.22% |                |
| **合计** | **6,777** | **5,422** | **1,355** | **19.9941%** | **27 组** |

## 7. 输出文件与字段

实施切分时生成以下两个新文件，不覆盖源 CSV：

- `data_output/mimic_similar.csv`
- `data_output/mimic_test.csv`

两个输出文件均不得包含本节未列出的额外字段，字段顺序也必须与本节完全一致。

### 7.1 `mimic_similar.csv`

CSV 表头必须为：

```csv
subject_id,hadm_id,admittime,seq_num,icd_code,icd_version,chief_complaint,major_surgical_or_invasive_procedure,history_of_present_illness,past_medical_history,social_history,family_history,physical_exam,pertinent_results,brief_hospital_course,medications_on_admission,discharge_medications,discharge_disposition,discharge_diagnosis,discharge_condition,discharge_instructions
```

其中：

- `subject_id`、`hadm_id`、`admittime`、`seq_num`、`icd_code`、`icd_version` 直接复制自源文件的同名字段，字段值不得改写；
- 其余 15 个字段从同一条源记录的 `text` 字段中按第 8.1 节规则提取；
- 不输出源文件的原始 `text` 字段。

### 7.2 `mimic_test.csv`

CSV 表头必须为：

```csv
subject_id,hadm_id,seq_num,icd_code,icd_version,discharge_text_before_disposition
```

其中：

- `subject_id`、`hadm_id`、`seq_num`、`icd_code`、`icd_version` 直接复制自源文件的同名字段，字段值不得改写；
- `discharge_text_before_disposition` 根据同一条源记录的 `text` 字段按第 8.2 节生成；
- 按本设计，`mimic_test.csv` **不包含** `admittime` 和原始 `text` 字段。

## 8. `text` 字段转换规则

所有文本处理均在完成数据切分后逐行进行，不得用结构化字段内容或文本长度参与 similar/test 分组，以免改变第 4～6 节定义的切分结果。

### 8.1 `mimic_similar.csv` 的段落提取

目标字段与源 `text` 中段落标题的映射如下：

| 输出字段 | 源段落标题 |
| --- | --- |
| `chief_complaint` | `Chief Complaint:` |
| `major_surgical_or_invasive_procedure` | `Major Surgical or Invasive Procedure:` |
| `history_of_present_illness` | `History of Present Illness:` |
| `past_medical_history` | `Past Medical History:` |
| `social_history` | `Social History:` |
| `family_history` | `Family History:` |
| `physical_exam` | `Physical Exam:` |
| `pertinent_results` | `Pertinent Results:` |
| `brief_hospital_course` | `Brief Hospital Course:` |
| `medications_on_admission` | `Medications on Admission:` |
| `discharge_medications` | `Discharge Medications:` |
| `discharge_disposition` | `Discharge Disposition:` |
| `discharge_diagnosis` | `Discharge Diagnosis:` |
| `discharge_condition` | `Discharge Condition:` |
| `discharge_instructions` | `Discharge Instructions:` |

#### 8.1.1 全量标题审计结果

已对当前源文件全部 6,777 条 `text` 逐行扫描，并结合相邻段落顺序抽查所有脱敏或残缺标题。下表中的“标准标题”是忽略大小写和水平空白后，能够匹配独立的完整标题及冒号的记录数；“特殊规则新增”只计算原本不能由标准标题识别、但能由第 8.1.2～8.1.3 节的保守规则可靠恢复的记录。

| 输出字段 | 标准标题 | 特殊规则新增 | 最终可解析 | 无可靠标题 | 当前文件中实际采用的特殊情况 |
| --- | ---: | ---: | ---: | ---: | --- |
| `chief_complaint` | 6,372 | 240 | 6,612 | 165 | `___ Complaint:`；规则同时兼容 `_____ complaint.` |
| `major_surgical_or_invasive_procedure` | 6,759 | 15 | 6,774 | 3 | `Major ___ or Invasive Procedure:` 及 `___ Surgical or Invasive Procedure:` |
| `history_of_present_illness` | 6,569 | 18 | 6,587 | 190 | `___ of Present Illness:` |
| `past_medical_history` | 6,556 | 6 | 6,562 | 215 | `___ Medical History:` 3 条；上下文有效的 `PMH:` 3 条 |
| `social_history` | 6,466 | 20 | 6,486 | 291 | 按顺序判定的 `___ History:` 10 条；`SH:`、同行内容或无冒号标题共 10 条 |
| `family_history` | 6,405 | 25 | 6,430 | 347 | 按顺序判定的 `___ History:` 16 条；`FH:`、同行内容或无冒号标题共 9 条 |
| `physical_exam` | 6,360 | 195 | 6,555 | 222 | `Physical ___:`、`___ Exam:` 共新增 194 条；`Admission Physical Exam:` 回退新增 1 条 |
| `pertinent_results` | 6,627 | 5 | 6,632 | 145 | 经位置校验的 `___ Results:` |
| `brief_hospital_course` | 5,949 | 36 | 5,985 | 792 | 粘连的 `___RIEF HOSPITAL COURSE` 26 条；其他缺冒号、`Hospital Course:` 或脱敏标题 10 条 |
| `medications_on_admission` | 6,342 | 246 | 6,588 | 189 | `___ on Admission:` |
| `discharge_medications` | 6,610 | 2 | 6,612 | 165 | `___ Medications:` 1 条；按顺序判定的 `Discharge ___:` 1 条 |
| `discharge_disposition` | 6,597 | 58 | 6,655 | 122 | `___ Disposition:` 55 条；按顺序判定的 `Discharge ___:` 2 条；粘连的 `___ischarge Disposition:` 1 条 |
| `discharge_diagnosis` | 6,640 | 110 | 6,750 | 27 | `___ Diagnosis:` 109 条；粘连的 `___ischarge Diagnosis:` 1 条 |
| `discharge_condition` | 6,775 | 2 | 6,777 | 0 | `___ Condition:` |
| `discharge_instructions` | 6,733 | 0 | 6,733 | 44 | 不采用模糊别名；`___ Instructions:` 在当前文件中实际表示后续随访说明 |

以上计数与第 2 节记录数和 SHA-256 绑定。源文件变化后必须重新审计，不能把本表作为其他版本数据的固定事实。表中的“无可靠标题”只表示无法安全定位该段落，不代表原文一定没有相关临床信息；这些记录按规则输出空字符串，不能从正文关键词猜测边界。

#### 8.1.2 允许的标题形式与封闭别名字典

标题检测使用原始 `text`，并保留每个候选标题在原文中的字符起止位置。仅为识别标题，可以忽略大小写和标题文字间的连续水平空白；不得先改写原文再切片。下文用 `R` 表示连续三个或更多下划线，即正则 `_{3,}`，因此既覆盖当前文件中的 `___`，也覆盖 `_____` 等长度的脱敏占位符。

完整标准标题仍是最高优先级。标准标题还允许以下两个保守变体，但只有在同一字段不存在完整独立标题且通过第 8.1.3 节顺序校验时才能使用：

- 整行只有完整标题文字但缺少冒号，例如 `BRIEF HOSPITAL COURSE`；
- 完整标题及冒号后在同一行直接出现内容，例如 `SOCIAL HISTORY: ___`。此时冒号后的文本是段落内容的一部分。

除标准标题外，只允许下列封闭别名，不得使用编辑距离、任意关键词或无约束的模糊匹配：

- `chief_complaint`：`R Complaint:` 或 `R Complaint.`；
- `major_surgical_or_invasive_procedure`：`Major R or Invasive Procedure:`、`R Surgical or Invasive Procedure:`；
- `history_of_present_illness`：`R of Present Illness:`；
- `past_medical_history`：`R Medical History:`，以及通过位置校验的独立 `PMH:` 或 `PMH/PSH:`；
- `social_history`：通过位置校验的 `SH:`，以及解析为 social 的 `R History:`；
- `family_history`：通过位置校验的 `FH:`，以及解析为 family 的 `R History:`；
- `physical_exam`：`Physical R:`、`Physical R`、`R Exam:`；仅在这些标题和标准标题都不存在时，才允许 `Admission Physical Exam:`、`Physical Exam on Admission:`、`Admission Exam:` 及其 `Examination` 变体作为回退；
- `pertinent_results`：通过位置校验的 `R Results:`；
- `brief_hospital_course`：`R Hospital Course:`、`Hospital Course:`、`Hospital Course by Problem:`，以及缺少首字母并可能粘在化验行末尾的 `Rrief Hospital Course` 或 `Rrief Hospital Course Template`；
- `medications_on_admission`：`R on Admission:`；
- `discharge_medications`：`R Medications:`，以及解析为 discharge medications 的 `Discharge R:`；
- `discharge_disposition`：`R Disposition:`、解析为 disposition 的 `Discharge R:`，以及缺少首字母并与前文粘连的 `Rischarge Disposition:`；
- `discharge_diagnosis`：通过位置校验的 `R Diagnosis:`，以及缺少首字母并与前文粘连的 `Rischarge Diagnosis:`；
- `discharge_condition`：通过位置校验的 `R Condition:`；
- `discharge_instructions`：不增加脱敏别名。当前文件中的 `R Instructions:` 均位于标准 `Discharge Instructions:` 之后，实际是脱敏后的 `Followup Instructions:`，只能作为 `discharge_instructions` 的末尾边界，不能写入该字段。

别名中的冒号必须存在，只有 `chief_complaint` 按用户给出的实际需求额外接受句点。普通正文中的 `complaint`、`history`、`course`、`diagnosis` 等词不能成为标题。

#### 8.1.3 候选消歧、优先级与内容边界

1. 第一遍只识别 15 个完整标准标题和完整 `Followup Instructions:`，建立高置信度锚点及其原文字符位置。
2. 某字段已有完整标准标题时，忽略该字段的缩写、脱敏标题、无冒号标题和回退标题，防止把段落内部的 `PMH:`、`Admission Physical Exam:` 或病理小标题重复提取。
3. 第二遍仅从第 8.1.2 节封闭字典中补充缺失字段。候选位置必须符合标准章节顺序：`Chief Complaint` → `Major Surgical or Invasive Procedure` → `History of Present Illness` → `Past Medical History` → `Social History` → `Family History` → `Physical Exam` → `Pertinent Results` → `Brief Hospital Course` → `Medications on Admission` → `Discharge Medications` → `Discharge Disposition` → `Discharge Diagnosis` → `Discharge Condition` → `Discharge Instructions` → `Followup Instructions`。候选必须位于已经解析出的最近前序锚点之后、最近后序锚点之前。
4. `R History:` 必须按邻接关系消歧：位于已解析的 `past_medical_history` 之后且 `family_history` 之前时解析为 `social_history`；位于 `social_history` 之后且 `physical_exam` 或 `pertinent_results` 之前时解析为 `family_history`。不满足这两种关系时保持未解析，不能同时写入两个字段。
5. `Discharge R:` 必须按邻接关系消歧：位于 `medications_on_admission` 或 `brief_hospital_course` 之后、`discharge_disposition` 之前，且下一高置信度标题是 `Discharge Disposition:` 时，才可解析为 `discharge_medications`；位于已解析的 `discharge_medications` 之后且下一高置信度标题是 `Discharge Diagnosis:` 时，才可解析为 `discharge_disposition`。位于 `Pertinent Results` 内并跟随化验值的 `DISCHARGE R:` 是出院化验子标题，不能解析成上述任一字段；位于 `Brief Hospital Course` 后并跟随门诊待办事项的同形标题也不能当作出院用药。
6. `R Diagnosis:` 只有位于已解析的 `discharge_disposition` 之后、`discharge_condition` 之前时才是 `discharge_diagnosis`。当前文件中另有病理结果里的 `R DIAGNOSIS:`，因位置不符必须保留在 `pertinent_results` 正文中。
7. `R Results:` 只有位于 `physical_exam` 或 `family_history` 之后，且位于 `brief_hospital_course` 或 `medications_on_admission` 之前时才是 `pertinent_results`。单独的 `R Course:` 常表示急诊或外院病程，不是 `brief_hospital_course`，不得采用。
8. 对 `Rrief Hospital Course`、`Rischarge Disposition:` 和 `Rischarge Diagnosis:` 这三类粘连标题，`R` 通常同时承担前一段末尾的脱敏占位符。切分边界从可见残缺标题的首字符 `rief` 或 `ischarge` 开始，前面的下划线仍保留在前一字段中；标题内容从残缺标题末尾之后开始。不得从整行行首截断，否则会丢失同一行前面的化验或临床内容。
9. 标题优先级依次为：完整独立标准标题、完整标准标题的同行内容/缺冒号变体、非歧义脱敏别名、经上下文消歧的别名、入院查体等回退标题。低优先级候选不得覆盖高优先级候选。
10. 将所有最终接受的目标标题以及标准或脱敏后的 `Followup Instructions` 作为顶层边界。某一段落的内容从标题匹配结束位置开始，到下一个已接受边界标题的匹配开始位置之前结束；若后面没有边界标题，则到 `text` 结尾结束。
11. 标题本身不写入输出字段。只去除段落内容首尾用于分隔段落的空白字符，正文内部的空格、标点和换行保持不变。同一行标题后的内容不得丢失。
12. 若同一字段出现多个同优先级且均通过顺序校验的目标标题，按原文顺序提取各个非空内容，并使用两个换行符 `\n\n` 连接。低优先级别名不参与重复段落拼接。
13. 若某个目标标题不存在、所有候选均未通过消歧，或标题下没有非空内容，对应字段写入空字符串；无论是否缺少段落，CSV 中都必须保留全部 15 个结构化字段。
14. 不能把独立的 `R`、任意全大写短行或看似标题的正文当作段落边界。宁可将不能可靠判断的字段留空，也不能跨段落污染其他结构化字段。
15. 含逗号、双引号或换行的内容必须按标准 CSV 规则正确转义，不得用删除换行或替换标点的方式规避转义。
16. 质量报告必须分别记录每种标准标题、别名、消歧结果、拒绝候选和最终空字段的记录数及 `(subject_id, hadm_id)`，不能只报告汇总缺失率。

### 8.2 `mimic_test.csv` 的文本截断

对每一条 test 记录执行以下确定性规则：

1. 优先查找原始 `text` 中最早出现的独立完整标题行 `Discharge Medications:`。若完整标题不存在，再使用第 8.1.2～8.1.3 节中已经消歧为 `discharge_medications` 的高置信度别名；不能仅凭 `Discharge R:` 的文字形状直接截断。
2. 若找到完整标题或已消歧别名，`discharge_text_before_disposition` 取从原始 `text` 开头到该标题匹配起点之前的**精确原文前缀**；从标题匹配起点开始直至 `text` 结尾的全部内容均删除。因此输出中既不能包含该标题，也不能包含标题后的药物、处置、诊断、病情、医嘱等任何内容。
3. 截断后的前缀不得执行 `strip`、换行归一化、空白压缩或其他改写；除被删除的后缀外，其字符序列必须与原始 `text` 前缀完全一致。
4. 若原始 `text` 中既不存在完整标题，也不存在通过消歧的高置信度别名，则没有可靠的截断位置，`discharge_text_before_disposition` 写入完整原始 `text`，并在质量报告中记录为 `missing_discharge_medications_heading`；不得改用 `Discharge Disposition:` 或其他标题猜测截断位置。

注意：字段名固定为 `discharge_text_before_disposition`，但本设计规定的实际截断点是已解析的 `discharge_medications` 段落标题，而不是 `Discharge Disposition:` 标题行。

## 9. 验收标准

实现完成后必须同时满足：

1. similar 恰为 5,422 条，test 恰为 1,355 条，总体占比为 80.0059%:19.9941%；
2. 两集合的 `(subject_id, hadm_id)` 交集为空，并集与输入记录键完全一致；
3. 两集合内均无重复记录键，且记录总数之和为 6,777；
4. 82 个整数可行组的 test 占比全部位于闭区间 `[15%, 25%]`；
5. 27 个整数不可行组必须与第 3 节的最近整数方案一致，并在质量报告中单独标记，不能伪装成通过比例检查；
6. 每个 `icd3` 的 similar 数、test 数与第 6 节基线一致；
7. 使用相同源文件和算法版本重复运行，manifest 和两个输出 CSV 的 SHA-256 完全一致；
8. 两个 CSV 的表头和字段顺序分别与第 7.1、7.2 节完全一致，且不包含未声明字段；
9. 切分前后各 `icd3` 数量守恒：`input_count = similar_count + test_count`；
10. 若输入文件 SHA-256、记录数或疾病分布变化，必须重新计算配额并更新质量基线后再验收。
11. 从源文件复制的字段值不被规范化逻辑改写；`mimic_similar.csv` 中每个结构化字段均符合第 8.1 节的标题、边界、缺失值和换行保留规则。
12. 对存在完整 `Discharge Medications:` 标题或已按第 8.1.3 节消歧为 `discharge_medications` 别名的每条 test 记录，`discharge_text_before_disposition` 必须等于该标题匹配起点之前的原始 `text` 精确前缀，且不得包含标题及其后的任何字符。
13. 对既不存在完整标题、也不存在已消歧高置信度别名的 test 记录，`discharge_text_before_disposition` 必须与完整原始 `text` 完全一致，并在质量报告中逐条列出记录键。
14. 质量报告至少包含 15 个目标段落各自的标准标题数、各别名接受数、歧义候选接受/拒绝数、最终空字段数，以及 `missing_discharge_medications_heading` 的数量和 `(subject_id, hadm_id)` 明细。
15. 对完整源文件执行标题审计时，第 8.1.1 节 15 个字段的“标准标题”“特殊规则新增”“最终可解析”和“无可靠标题”计数必须逐项一致；任一计数变化都应阻止验收并触发源文件版本检查。
16. 当前文件中的 26 个 `R History:` 必须恰好解析为 10 个 `social_history` 和 16 个 `family_history`；`R Instructions:` 不得解析为 `discharge_instructions`。位于病理结果中的 `R DIAGNOSIS:`、位于化验区的 `DISCHARGE R:` 和表示急诊/外院病程的 `R Course:` 均不得误建目标段落。
17. 对 26 条粘连的 `Rrief Hospital Course` 记录，`brief_hospital_course` 必须成功提取；粘连标题之前同一行的化验文本及下划线占位符必须完整保留在前一字段中。
