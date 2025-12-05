# 🏥 新加坡智能医疗搜索 | Agent Agentic Medical Search (Singapore) 

这是一个基于 Python Streamlit 和 LLM (Large Language Model) 构建的智能医疗搜索应用。它采用 Agentic Workflow（代理工作流），能够理解用户的自然语言查询（如症状、地点、模糊人名），并将其转化为结构化的搜索指令，结合模糊匹配和地理位置算法，在新加坡的医生与诊所的指定数据库中进行精准检索。

## ⚠️ 隐私与数据声明 / Privacy & Data Disclaimer

出于隐私保护和数据合规性原因：

1. 不提供数据文件：本项目仓库不包含任何真实的医生或诊所数据文件（Excel/CSV）。

2. 不包含 API Key：代码中未硬编码任何 API 密钥。用户需要在侧边栏自行输入兼容 OpenAI 格式的 API Key（推荐使用 SiliconFlow/DeepSeek）。

## ✨ 主要功能

### 智能意图识别 (LLM Powered)

- 使用 LLM (默认 DeepSeek-V3) 分析用户自然语言。

- 识别查询意图（找医生 vs 找诊所）。

- 从描述中提取关键信息：症状（自动映射到专科）、地点（区域/邮编）、语言要求。

### 混合搜索算法

- 硬过滤 (Hard Filter)：基于 Pandas 的精确筛选（专科、区域）。

- 模糊匹配 (Fuzzy Match)：使用 RapidFuzz 处理拼写错误或不完整的人名搜索。

### 交互式地图可视化

- 自动地理编码 (Geocoding) 将地址转换为坐标。

- 支持新加坡邮政编码 (Postal Code) 的距离计算逻辑。
  
- 在地图上展示诊所位置，并标记距离查询点的远近。

### 动态数据映射

- 智能识别上传 Excel/CSV 的列名，无需严格修改表头即可适配。

## 🛠️ 系统架构
该项目采用 "Think-Filter-Rank" 的处理流程：

1. User Query: 用户输入自然语言 (例如: "我打篮球骨折了，需要会说中文的医生").

2. Agent Think (LLM): LLM 解析意图，生成 JSON 指令:

### JSON
```JSON
{
  "intent": "find_doctor"
  "filters": {
    "Specialty": "Orthopaedic Surgery"
    "Languages": "Chinese"
    "Area": ""
  }
}
```
3. Filter (Pandas): 根据 JSON 指令对 DataFrame 进行初步筛选。

4. Fuzzy & Rank: 对剩余结果进行名称模糊匹配。

5. Response: 生成结果卡片。


## 🚀 快速开始
1. 环境准备
确保你已安装 Python 3.13+。

```
Bash

git clone https://github.com/aslzhixixi/sg-medical-agent.git
cd sg-medical-agent

## 安装依赖
pip install -r requirements.txt
```
2. 运行应用
```
Bash

streamlit run SEARCHING.py
```
3. 使用指南
- 配置 API: 在左侧侧边栏输入你的 API Key (代码默认适配 SiliconFlow API，模型为 DeepSeek-V3)。

- 上传数据: 上传两个数据文件（支持 CSV 或 Excel）：

    - Clinic Data: 包含诊所名称、地址、区域等信息。

    - Doctor Data: 包含医生姓名、专科、语言、服务等信息。

- 开始搜索: 在底部聊天框输入问题，例如：

    - "Find Dr. Tan"

    - "Nearest clinic to 179094"

## 📦 依赖列表 (Requirements)
请确保你的 requirements.txt 包含以下库：

```
streamlit
pandas
rapidfuzz
folium
streamlit-folium
geopy
openai
openpyxl
```

注意: 本项目仅供学习与研究用途，不构成医疗建议。
