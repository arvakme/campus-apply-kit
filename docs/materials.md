# 材料准备

所有材料放在实例目录 `$CAMPUS_INSTANCE/materials/`，不进 git。worker 上传时只从这里取，路径写进工单和留痕时用 `materials/<文件名>`。

## 1. 清单

**标"必备"的是实测被门户拦过提交或红线的**，缺了会被拦停；怎么办理看"在哪办"列。

### 1.1 必备

| 材料 | 文件名 | 规格 | 在哪办 / 办理耗时 |
|---|---|---|---|
| 中文简历 | `resume-cn.pdf` | PDF，可提取文字，≤ 2MB | 维护者自备；更新即覆盖 |
| 英文简历 | `resume-en.pdf` | 同上，内容与中文版一致 | 与中文版同步改 |
| 证件照 | `id-photo.jpg` | 白底免冠 JPG，原图保留 | 照相馆/自拍抠图；即办 |
| 证件照压缩版 | `id-photo-500k.jpg`、`id-photo-100k.jpg` | 见 §3，多备几档 | `sips` 压，分钟级 |
| 生活照 | `life-photo.jpg` | 半身/全身 JPG，≤ 200KB | 自备；即办 |
| 身份证正反面 | `id-card-front.jpg`、`id-card-back.jpg` | 清晰扫描/拍照，四角完整无反光，JPG/PDF | 手机扫描 App；即办 |
| 学籍在线验证报告 | `xueji-report.pdf` | 学信网 PDF，注意有效期（1-6 个月自选） | 学信网→学信档案→在线验证报告→教育部学籍在线验证报告；即办 |
| 学历在线验证报告 | `xueli-report.pdf` | 同上入口选"教育部学历证书电子注册备案表" | 已毕业学历用；即办 |
| 毕业证扫描件 | `diploma-bachelor.pdf` 等（按学历命名） | 证书原件扫描 PDF/JPG | 扫原件；即办 |
| 学位证扫描件 | `degree-bachelor.pdf` 等 | 同上 | 同上 |
| 成绩单（中文+英文，各学历） | `transcript-bachelor.pdf`、`transcript-master.pdf` | 学校盖章/官方电子版 PDF | 国内校教务处自助打印（即办）；海外校官方 transcript 申请数天 |
| 外语成绩证明 | `cet6.pdf`、`cet4.pdf`、`ielts.pdf` 等 | 成绩报告单扫描件 | 四六级官网补办成绩证明免费邮寄（数天）；IELTS 官网电子送分/成绩单扫描 |
| 学生证 | `student-id.pdf` | 学生证照片页+注册页扫描 | 扫原件；即办 |
| 在读证明 | `enrollment-cert.pdf` | 学校盖章版；海外院校开在读证明/CoE | 国内教务处即办；海外院校数天 |
| 留服认证书（海外学历必备） | `liufu-cert.pdf` | 留服中心 PDF 认证书 | zwfw.cscse.edu.cn 网上大厅申请，**约 1-2 个月，提前办** |

### 1.2 可选

| 材料 | 文件名 | 规格 | 在哪办 / 耗时 |
|---|---|---|---|
| 获奖证书 | `award-01-<简称>.jpg` 按重要度编号 | 扫描件 JPG/PDF | 扫原件；即办 |
| 实习证明 | `intern-proof-<单位简称>.pdf` | 盖章证明扫描 | 实习单位 HR，数天 |
| 党员证明 | `party-proof.pdf` | 党组织盖章证明 | 所在党支部，数天 |
| 推荐信 | `ref-<推荐人>.pdf` | 签名扫描 | 推荐人，数天 |
| 软著/专利证书 | `patent-<简称>.pdf` | 证书扫描 | 扫原件 |
| 其他附件打包 | 按需临时打包 | zip/rar；门户指定命名时先复制改名再传 | 门户要求时临时做 |

“可选”材料是否准备，决定了 `intent.policies.attachments_required` 该填 `hold` 还是 `skip`。经验：国企、银行、研究院的网申常强制上传学生证、成绩单、身份证扫描件，缺了就提交不了，只能先标"待材料"等补齐再续投。

命名约定：

- 只用英文小写、数字、连字符，不用空格和中文。部分门户对中文文件名解析出错，也方便命令行操作。
- 例外：门户或公告要求附件按“姓名-学校-岗位”命名时，上传前复制一份按要求改名，原件不动。邮件附件同理。
- 更新简历时直接覆盖同名文件，并在 `profile.md` 注明更新日期。

## 2. 简历

- 必须是 PDF。HTML 版、在线链接不能当附件（维护者的 HTML 版有 50MB，门户直接拒）。
- 文字要可选中复制，不要纯图片 PDF。多数 ATS 会解析 PDF 自动填表，图片版解析不出来。
- 解析效果影响填表工作量：教育、实习、项目分段清晰，日期格式统一（`YYYY.MM—YYYY.MM`），项目名和描述分行。
- 简历里写清 GPA/均分和排名，否则 worker 遇到必填成绩字段只能 blocked。
- 外企版英文简历内容和中文版保持一致，不要多写。

## 3. 证件照

- 白底、免冠、正面、近期。原图保留在 `id-photo.jpg`。
- 常见门户上限：
  - 500KB（海康系门户实测）
  - 1MB（电信系门户实测）
  - 部分银行门户需要更小，实测压到十几 KB 通过
- macOS 自带 `sips` 压缩：

```bash
cd $CAMPUS_INSTANCE/materials
# 长边缩到 640px，JPEG 质量 80，一般落在 50–100KB
sips -Z 640 -s format jpeg -s formatOptions 80 id-photo.jpg --out id-photo-500k.jpg
# 更小：长边 500px，质量 60
sips -Z 500 -s format jpeg -s formatOptions 60 id-photo.jpg --out id-photo-100k.jpg
ls -lh id-photo*.jpg
# 查看像素尺寸
sips -g pixelWidth -g pixelHeight id-photo-500k.jpg
```

- 有的门户限定像素比例（如 3:4、宽高范围），不满足时用 `sips -z <高> <宽>` 强制尺寸，再检查是否变形。
- 压缩副本放在 `materials/` 里，不要放 `/tmp`。维护者踩过坑：路径写错时组件照样回显文件名，但文件根本没传上去。

## 4. 生活照

- 半身或全身，背景干净。部分银行和国企必填。
- 一般 ≤ 200KB 能直接用；超了按 §3 压缩，长边 1440px 足够。

## 5. 附加材料

- 身份证、学生证、成绩单、在读证明：扫描或手机扫描 App 生成 PDF/JPG，四角完整、无反光。
- 学籍在线验证报告：学信网申请，注意报告有有效期，过期重新下载。
- 获奖证书：同时把奖项全称、级别（国家级 / 省部级 / 院校级）、获奖日期写进问题库，表单填文字时用。

## 6. host-check 会检查什么

`host/host-check.py` 尚在规划中。落地后按下面的规则检查（web `/setup` 页同源）：

| 检查项 | 通过标准 | 级别 |
|---|---|---|
| `materials/` 存在 | 目录存在 | 必需 |
| `resume-cn.pdf` | 存在，是 PDF，≤ 10MB，能提取出文字 | 必需 |
| `resume-en.pdf` | `intent.policies.foreign_cv: en` 时必需 | 条件必需 |
| `id-photo.jpg` | 存在，JPG | 必需 |
| 证件照压缩版 | 至少一个 ≤ 500KB 的副本 | 必需（实测被拦） |
| `life-photo.jpg` | 存在 | 必需（银行国企必填） |
| `id-card-front/back.jpg` | 存在 | 必需（国企、银行类门户常强制） |
| `xueji-report.pdf` / `student-id.pdf` / `enrollment-cert.pdf` | 至少一种学生身份证明存在 | 必需（国企、银行类门户常强制） |
| `transcript-*.pdf` | 各学历至少一份成绩单 | 必需（国企、银行类门户常强制） |
| `cet4/6.pdf` 或 `ielts.pdf` | 与问题库 english-* 条目对应的成绩证明 | 必需（部分运营商、国企门户强制） |
| `liufu-cert.pdf` | 有海外学历时必需 | 条件必需（公告级） |
| `diploma-*.pdf` / `degree-*.pdf` | 已毕业学历的证书扫描 | 警告（编号核对用） |
| 可选材料 | `attachments_required: hold` 时列出缺哪些 | 提示 |
| 文件名 | 不含空格和中文 | 警告 |

在它落地前，手动检查：

```bash
cd $CAMPUS_INSTANCE/materials && ls -lh
file resume-cn.pdf id-photo.jpg
python3 -c "import os;[print(f, os.path.getsize(f)//1024, 'KB') for f in sorted(os.listdir('.'))]"
```
