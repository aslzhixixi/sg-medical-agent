import streamlit as st
import pandas as pd
import json
import os
from rapidfuzz import process, fuzz
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
import time

# --- 1. 基础设置与依赖检查 ---
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

st.set_page_config(
    page_title="Agentic 医疗搜索",
    page_icon="🩺",
    layout="wide"
)

# --- 2. CSS 样式优化 ---
st.markdown("""
<style>
    .stChatMessage { background-color: #f4f6f9; border-radius: 10px; border: 1px solid #e1e4e8; }
    .result-card {
        background-color: white; padding: 20px; border-radius: 12px;
        border-left: 6px solid #10a37f; /* ChatGPT Green */
        box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 15px;
    }
    .tag-container { margin-top: 8px; }
    .tag {
        display: inline-block; padding: 4px 12px; border-radius: 20px;
        font-size: 0.85em; font-weight: 500; margin-right: 6px; margin-bottom: 6px;
    }
    .tag-spec { background-color: #e3f2fd; color: #1565c0; } /* 蓝色: 专科 */
    .tag-loc { background-color: #f3e5f5; color: #7b1fa2; } /* 紫色: 地点 */
    .tag-lang { background-color: #e8f5e9; color: #2e7d32; } /* 绿色: 语言 */
    .debug-expander { background-color: #fff8e1; border: 1px dashed #ffc107; border-radius: 5px; }
</style>
""", unsafe_allow_html=True)

class MedicalAgent:
    def __init__(self):
        self.client = None
        self.model = "deepseek-ai/DeepSeek-V3" # 默认推荐模型

    def connect_api(self, api_key, base_url):
        if not HAS_OPENAI: return False, "未安装 openai 库"
        try:
            self.client = OpenAI(api_key=api_key, base_url=base_url)
            # 测试连接
            self.client.models.list()
            return True, "连接成功"
        except Exception as e:
            return False, str(e)

    @st.cache_data(ttl=3600)
    def load_data(_self, file_c, file_d):
        """智能加载数据，自动标准化列名"""
        try:
            # 检查xlsx文件依赖
            if not HAS_OPENPYXL and (file_c.name.endswith('.xlsx') or file_d.name.endswith('.xlsx')):
                st.error("❌ 需要安装 openpyxl 来读取 .xlsx 文件，请运行: pip install openpyxl")
                return None, None
                
            # 读取文件辅助函数
            def read_file(f):
                if isinstance(f, str): return pd.read_csv(f) if f.endswith('.csv') else pd.read_excel(f)
                return pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)

            df_c = read_file(file_c)
            df_d = read_file(file_d)
           
            # 使用object类型来避免dtype兼容性警告
            df_c = df_c.fillna('')
            df_d = df_d.fillna('')

            # === 核心优化：建立列名映射字典 ===
            # 目的是让代码里的 'Name', 'Area' 能对应上 Excel 里千奇百怪的表头
           
            # 医生表映射 - 基于实际文件结构
            d_map = {}
            for col in df_d.columns:
                cl = col.lower()
                if 'doctor name' in cl or 'name' in cl: d_map[col] = 'Name'
                elif 'specialty' in cl: d_map[col] = 'Specialty'
                elif 'languages spoken' in cl or 'language' in cl: d_map[col] = 'Languages'
                elif 'services' in cl: d_map[col] = 'Services'
                elif 'qualifications' in cl: d_map[col] = 'Qualifications'
                elif 'designation' in cl: d_map[col] = 'Designation'
           
            # 诊所表映射 - 基于实际文件结构  
            c_map = {}
            for col in df_c.columns:
                cl = col.lower()
                if 'gp clinic name' in cl or 'clinic name' in cl: c_map[col] = 'Name'
                elif 'clinic address' in cl or 'address' in cl: c_map[col] = 'Address'
                elif 'area' in cl: c_map[col] = 'Area'

            if d_map: df_d.rename(columns=d_map, inplace=True)
            if c_map: df_c.rename(columns=c_map, inplace=True)

            # 统一转字符串
            for df in [df_c, df_d]:
                for col in df.columns: df[col] = df[col].astype(str)

            return df_c, df_d
        except Exception as e:
            return None, None

    def think(self, query):
        """
        Agent 思考阶段：意图识别与参数提取
        这是 'Agentic' 的核心，利用 LLM 将自然语言转化为结构化指令
        """
        if not self.client: return None

        system_prompt = """
        You are a medical search intent analyzer.
        Target Data:
        1. Doctors (Fields: Name, Specialty, Languages, Services)
        2. Clinics (Fields: Name, Address, Area)

        Task: Parse user query into a JSON object.
       
        Logic for parsing:
        1. ***LOCATION SEARCH PRIORITY***: If query contains "nearest", "closest", "near", "around", "离...最近" patterns, set intent="find_clinic" and extract location to "Area" field.
        2. ***NAME DETECTION***: If query contains patterns like "find dr. [name]", "doctor [name]", or specific names, extract to "keywords" field and leave "Specialty" EMPTY.
        3. Location extraction: Singapore areas like "Bedok", "Tampines", "Yishun", "Ang Mo Kio", "Woodlands", etc. -> "Area" field
        4. Language extraction: "Chinese", "Mandarin", "English" etc. -> "Languages" field  
        5. ***SPECIALTY FROM SYMPTOMS*** (only if NO specific name mentioned): ONLY use these EXACT names that exist in database:
          - "fever/cold/flu/general illness/sick" -> "General Medicine" (NOT "General Practitioner")
          - "baby/kid/child/infant" -> "Family & Community Medicine"
          - "emergency/urgent/serious" -> "Emergency Medicine"
          - "heart/chest pain/cardiac" -> "Cardiology"
          - "stomach/gut/digestive" -> "Gastroenterology"
          - "bone/fracture/injury" -> "Orthopaedic Surgery"
          - "eye/vision" -> "Ophthalmology"
          - "throat/ear/nose" -> "Otolaryngology"
          - "mental/depression/anxiety" -> "Psychiatry"
          - "tooth/teeth/dentist" -> "Dental"
          - "diabetes/sugar" -> "Endocrinology"
          - "kidney/renal" -> "Renal Medicine"
          - "urine/bladder" -> "Urology"
          - "breathing/lung" -> "Respiratory Medicine"
          - Default: "General Medicine" for common symptoms
       
        Output JSON Format:
        {
            "intent": "find_doctor" or "find_clinic",
            "keywords": "Specific name of person or clinic (leave empty if general search)",
            "filters": {
                "Specialty": "...",
                "Languages": "...",
                "Area": "..."
            },
            "reasoning": "Brief explanation of inference"
        }
        
        Examples:
        - "nearest clinic to Bedok" -> intent: "find_clinic", keywords: "", Area: "Bedok" (location-based clinic search)
        - "clinics near Tampines" -> intent: "find_clinic", keywords: "", Area: "Tampines" (location-based clinic search)
        - "clinic nearest 641652" -> intent: "find_clinic", keywords: "", Area: "641652" (postal code-based search)
        - "i want clinic nearest 560123" -> intent: "find_clinic", keywords: "", Area: "560123" (postal code search)
        - "find dr. low" -> intent: "find_doctor", keywords: "low", Specialty: "" (doctor name search)
        - "find doctor smith" -> intent: "find_doctor", keywords: "smith", Specialty: "" (doctor name search)
        - "i want jam avin" -> intent: "find_doctor", keywords: "jam avin", Specialty: "" (doctor name search)
        - "i have fever" -> intent: "find_doctor", keywords: "", Specialty: "General Medicine" (symptom-based search)
        - "baby sick" -> intent: "find_doctor", keywords: "", Specialty: "Family & Community Medicine" (symptom-based search)
        """

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query}
                ],
                response_format={"type": "json_object"},
                temperature=0.1 # 降低随机性，保证 JSON 格式稳定
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            st.error(f"Agent 思考失败: {e}")
            return None

    def search(self, df_c, df_d, query):
        # 检查数据是否加载成功
        if df_c is None or df_d is None:
            return [], {"error": "数据文件未正确加载，请检查上传的文件格式"}
            
        # 1. 思考 (Think)
        plan = self.think(query)
        if not plan:
            return [], "API 未连接或思考失败，无法执行智能搜索。"

        intent = plan.get('intent', 'find_doctor')
        filters = plan.get('filters', {})
        keywords = plan.get('keywords', '')
        reasoning = plan.get('reasoning', '')

        # 准备数据源
        if intent == 'find_clinic':
            target_df = df_c.copy()
            rtype = 'Clinic'
        else:
            target_df = df_d.copy()
            rtype = 'Doctor'

        # 2. 结构化过滤 (Filter) - Pandas 硬筛选
        # 这一步保证了准确性 (Precision)
        filtered_df = target_df
       
        # 专科筛选 - 修正AI可能输出的错误专科名称
        if filters.get('Specialty'):
            specialty_filter = filters['Specialty']
            
            # AI专科名称修正映射
            specialty_corrections = {
                'General Practitioner': 'General Medicine',
                'GP': 'General Medicine', 
                'Family Medicine': 'Family & Community Medicine',
                'Paediatric': 'Family & Community Medicine',  # 儿科查询修正
                'Pediatric': 'Family & Community Medicine',
                'ENT': 'Otolaryngology',
                'Orthopaedic': 'Orthopaedic Surgery',
                'Orthopedic': 'Orthopaedic Surgery'
            }
            
            # 应用修正
            if specialty_filter in specialty_corrections:
                specialty_filter = specialty_corrections[specialty_filter]
            
            # 对于儿科查询，直接推荐全科医生等更适合的专科
            if specialty_filter.lower() in ['paediatric', 'pediatric']:
                # 儿科查询优先推荐全科、急诊、内科医生
                fallback_specialties = ['Family & Community Medicine', 'General Medicine', 'Emergency Medicine', 'Internal Medicine']
                fallback_matches = pd.Series([False] * len(filtered_df))
                for fallback in fallback_specialties:
                    if 'Specialty' in filtered_df.columns:
                        matches = filtered_df['Specialty'].str.contains(fallback, case=False, na=False)
                        fallback_matches = fallback_matches | matches
                filtered_df = filtered_df[fallback_matches]
                
                # 如果还是没找到，再搜索儿科专门服务
                if filtered_df.empty:
                    specialty_matches = pd.Series([False] * len(target_df))
                    search_columns = ['Specialty', 'Designation', 'Services']
                    for col in search_columns:
                        if col in target_df.columns:
                            matches = target_df[col].str.contains(specialty_filter, case=False, na=False)
                            specialty_matches = specialty_matches | matches
                    filtered_df = target_df[specialty_matches]
            else:
                # 非儿科查询，正常搜索
                specialty_matches = pd.Series([False] * len(filtered_df))
                search_columns = ['Specialty', 'Designation', 'Services']
                for col in search_columns:
                    if col in filtered_df.columns:
                        matches = filtered_df[col].str.contains(specialty_filter, case=False, na=False)
                        specialty_matches = specialty_matches | matches
                filtered_df = filtered_df[specialty_matches]
       
        # 语言筛选
        if filters.get('Languages') and 'Languages' in filtered_df.columns:
            # 处理 "Chinese" 这种统称
            lang = filters['Languages']
            if lang.lower() in ['chinese', 'mandarin']: lang = 'Mandarin' # 假设表里是 Mandarin
            filtered_df = filtered_df[filtered_df['Languages'].str.contains(lang, case=False, na=False)]

        # 智能地理位置筛选 - 针对诊所搜索优化，支持邮政编码
        loc_key = filters.get('Area')
        if loc_key and intent == 'find_clinic':
            # 检查是否为邮政编码（6位数字）
            if loc_key.isdigit() and len(loc_key) == 6:
                # 邮政编码搜索逻辑
                import re
                query_postal = int(loc_key)
                clinic_distances = []
                
                # 从地址中提取所有邮政编码并计算距离
                for idx, row in filtered_df.iterrows():
                    address = str(row.get('Address', ''))
                    postal_match = re.search(r'Singapore\s+(\d{6})', address)
                    if postal_match:
                        clinic_postal = int(postal_match.group(1))
                        # 使用更准确的距离计算
                        distance = self.calculate_postal_distance(query_postal, clinic_postal)
                        # 将row转换为字典并添加距离信息
                        clinic_data = dict(row)
                        clinic_data['_distance'] = distance
                        clinic_distances.append(clinic_data)
                
                # 按距离排序并取前20个
                if clinic_distances:
                    clinic_distances.sort(key=lambda x: x['_distance'])
                    closest_clinics = clinic_distances[:20]
                    filtered_df = pd.DataFrame(closest_clinics)
                else:
                    # 如果没有找到邮政编码，返回空结果
                    filtered_df = pd.DataFrame()
            else:
                # 常规区域名称搜索
                # 多层次地理匹配策略
                location_matches = pd.Series([False] * len(filtered_df))
                
                # 1. 精确区域匹配 (最高优先级)
                if 'Area' in filtered_df.columns:
                    exact_area_matches = filtered_df['Area'].str.contains(loc_key, case=False, na=False)
                    location_matches = location_matches | exact_area_matches
                
                # 2. 地址部分匹配 (用于更精确的位置搜索)
                if 'Address' in filtered_df.columns:
                    address_matches = filtered_df['Address'].str.contains(loc_key, case=False, na=False) 
                    location_matches = location_matches | address_matches
                
                # 3. 如果没有直接匹配，尝试邻近区域推荐
                if not location_matches.any():
                    # 新加坡邻近区域映射 (基于实际地理位置)
                    nearby_areas = {
                        'bedok': ['tampines', 'pasir ris', 'changi'],
                        'tampines': ['bedok', 'pasir ris', 'sengkang'],
                        'yishun': ['woodlands', 'sembawang', 'ang mo kio'],
                        'woodlands': ['yishun', 'sembawang', 'choa chu kang'],
                        'jurong west': ['jurong east', 'choa chu kang', 'bukit batok'],
                        'sengkang': ['punggol', 'tampines', 'serangoon'],
                        'punggol': ['sengkang', 'tampines', 'serangoon'],
                        'ang mo kio': ['yishun', 'serangoon', 'bishan'],
                        'serangoon': ['ang mo kio', 'sengkang', 'bishan']
                    }
                    
                    loc_key_lower = loc_key.lower()
                    if loc_key_lower in nearby_areas:
                        nearby_list = nearby_areas[loc_key_lower]
                        for nearby in nearby_list:
                            if 'Area' in filtered_df.columns:
                                nearby_matches = filtered_df['Area'].str.contains(nearby, case=False, na=False)
                                location_matches = location_matches | nearby_matches
                
                filtered_df = filtered_df[location_matches]
        elif loc_key and intent == 'find_doctor':
            # 医生搜索的地址筛选 (保持原逻辑)
            col_to_search = 'Area' if 'Area' in filtered_df.columns else 'Address'  
            if col_to_search in filtered_df.columns:
                filtered_df = filtered_df[filtered_df[col_to_search].str.contains(loc_key, case=False, na=False)]

        # 3. 模糊匹配 (Fuzzy Match) - RapidFuzz
        # 这一步保证了容错性 (Recall)
        results = []
       
        # 如果过滤后已经没数据了，就不用搜了
        if not filtered_df.empty:
            # 诊所搜索：按地理位置优先级排序
            if intent == 'find_clinic':
                if loc_key:
                    # 按地理相关性排序
                    exact_area = []
                    exact_address = []
                    nearby_area = []
                    
                    for _, row in filtered_df.iterrows():
                        area = str(row.get('Area', '')).lower()
                        address = str(row.get('Address', '')).lower()
                        loc_lower = loc_key.lower()
                        
                        # 精确区域匹配最优先
                        if loc_lower in area:
                            exact_area.append(row)
                        # 地址匹配次优先  
                        elif loc_lower in address:
                            exact_address.append(row)
                        # 邻近区域最后
                        else:
                            nearby_area.append(row)
                    
                    # 按优先级合并结果，每类最多10个
                    results = exact_area[:10] + exact_address[:5] + nearby_area[:5]
                else:
                    # 没有指定位置，返回前15个诊所
                    results = [row for _, row in filtered_df.head(15).iterrows()]
                    
            # 医生搜索：按姓名模糊匹配  
            elif keywords and len(keywords) > 1:
                # 多种模糊匹配策略，扩大搜索范围
                names = filtered_df['Name'].tolist()
                
                # 策略1: token_set_ratio (对单词顺序不敏感)
                matches1 = process.extract(keywords, names, limit=20, scorer=fuzz.token_set_ratio)
                
                # 策略2: partial_ratio (部分匹配) - 提高limit以捕获更多候选
                matches2 = process.extract(keywords, names, limit=20, scorer=fuzz.partial_ratio)
                
                # 策略3: token_sort_ratio (排序后匹配)
                matches3 = process.extract(keywords, names, limit=20, scorer=fuzz.token_sort_ratio)
                
                # 策略4: 专门处理多词姓名的部分匹配
                multi_word_matches = []
                keywords_words = keywords.lower().split()
                for i, name in enumerate(names):
                    name_words = name.lower().split()
                    # 检查keywords中的每个词是否在姓名中有部分匹配
                    word_match_scores = []
                    for kw in keywords_words:
                        best_word_score = 0
                        for nw in name_words:
                            if len(kw) >= 3:  # 只对长度>=3的词进行部分匹配
                                if kw in nw or nw in kw:
                                    best_word_score = max(best_word_score, 80)
                                else:
                                    score = fuzz.ratio(kw, nw)
                                    best_word_score = max(best_word_score, score)
                        word_match_scores.append(best_word_score)
                    
                    # 如果所有关键词都有合理匹配，计算总分
                    if len(word_match_scores) > 0 and min(word_match_scores) > 35:
                        avg_score = sum(word_match_scores) / len(word_match_scores)
                        multi_word_matches.append((name, avg_score, i))
                
                # 合并所有匹配结果，提高分数权重
                all_matches = {}
                for strategy_name, matches in [("token_set", matches1), ("partial", matches2), ("token_sort", matches3), ("multi_word", multi_word_matches)]:
                    for name, score, idx in matches:
                        if score > 25:
                            # 对不同策略给予不同权重，partial_ratio对精确匹配更敏感
                            weighted_score = score
                            if strategy_name == "multi_word" and score > 50:
                                weighted_score = score * 1.3  # 多词匹配策略权重最高
                            elif strategy_name == "partial" and score > 80:
                                weighted_score = score * 1.2  # 提升精确匹配的权重
                            elif strategy_name == "token_set" and score > 90:
                                weighted_score = score * 1.1  # 提升高质量token匹配
                                
                            if name not in all_matches or weighted_score > all_matches[name][0]:
                                all_matches[name] = (weighted_score, idx, score)  # 保存原始分数用于调试
                
                # 按加权分数排序，确保最匹配的在前面
                sorted_matches = sorted(all_matches.items(), key=lambda x: x[1][0], reverse=True)
                
                # 进一步优化：精确匹配优先
                exact_matches = []
                fuzzy_matches = []
                
                for name, (weighted_score, idx, original_score) in sorted_matches:
                    name_lower = name.lower()
                    keywords_lower = keywords.lower()
                    
                    # 检查是否是精确匹配（姓氏完全匹配）
                    name_parts = name_lower.split()
                    if any(keywords_lower == part or part.startswith(keywords_lower) for part in name_parts):
                        exact_matches.append((name, weighted_score, idx))
                    else:
                        fuzzy_matches.append((name, weighted_score, idx))
                
                # 精确匹配在前，模糊匹配在后，限制总数
                # 优先返回精确匹配，如果精确匹配够用就不要模糊匹配
                if len(exact_matches) >= 3:
                    final_matches = exact_matches[:5]  # 如果精确匹配多，最多取5个
                else:
                    # 精确匹配不够，补充一些高质量的模糊匹配
                    high_quality_fuzzy = [m for m in fuzzy_matches if m[1] > 60]  # 只要高分的模糊匹配
                    final_matches = exact_matches + high_quality_fuzzy[:3]  # 最多3个模糊匹配
                
                for name, score, idx in final_matches[:5]:  # 总数限制为5个
                    original_row = filtered_df.iloc[idx]
                    results.append(original_row)
                    
                # 如果仍然没有找到结果，尝试包含匹配
                if not results:
                    for i, row in filtered_df.iterrows():
                        name = str(row['Name']).lower()
                        if keywords.lower() in name:
                            results.append(row)
                            if len(results) >= 10:
                                break
            else:
                # 一般搜索 (没有具体姓名的医生搜索，如"儿科医生")
                limit = 10 if intent == 'find_doctor' else 15
                results = [row for _, row in filtered_df.head(limit).iterrows()]

        return results, plan
    
    def calculate_postal_distance(self, postal1, postal2):
        """
        计算新加坡邮政编码之间的距离
        新加坡邮政编码分布规律：
        - 前2位表示区域（01-99）
        - 后4位表示具体位置
        """
        # 提取前2位区域代码
        area1 = postal1 // 10000
        area2 = postal2 // 10000
        
        # 如果在同一区域，使用后4位数字差距
        if area1 == area2:
            return abs(postal1 - postal2)
        
        # 不同区域的距离映射（基于新加坡地理位置）
        area_distances = {
            # Central (01-09) - 市中心区域
            (1, 2): 1, (1, 3): 2, (1, 4): 3, (1, 5): 4, (1, 6): 5,
            (1, 7): 6, (1, 8): 7, (1, 9): 8, (1, 10): 9,
            
            # North (72-73, 75-82) - 北部区域
            (75, 76): 1, (75, 77): 2, (75, 78): 3, (75, 79): 4,
            (79, 80): 1, (80, 81): 1, (81, 82): 1,
            
            # South (10-16) - 南部区域  
            (10, 11): 1, (11, 12): 1, (12, 13): 1, (13, 14): 1,
            (14, 15): 1, (15, 16): 1,
            
            # East (46-52) - 东部区域
            (46, 47): 1, (47, 48): 1, (48, 49): 1, (49, 50): 1,
            (50, 51): 1, (51, 52): 1,
            
            # West (60-69) - 西部区域
            (60, 61): 1, (61, 62): 1, (62, 63): 1, (63, 64): 1,
            (64, 65): 1, (65, 66): 1, (66, 67): 1, (67, 68): 1,
            (68, 69): 1,
            
            # Northeast (53-59) - 东北部区域
            (53, 54): 1, (54, 55): 1, (55, 56): 1, (56, 57): 1,
            (57, 58): 1, (58, 59): 1,
        }
        
        # 检查直接映射
        area_pair = tuple(sorted([area1, area2]))
        if area_pair in area_distances:
            base_distance = area_distances[area_pair] * 10000
        else:
            # 默认跨区域距离
            base_distance = abs(area1 - area2) * 10000
        
        # 加上区域内的细分距离
        sub_distance = abs((postal1 % 10000) - (postal2 % 10000)) / 100
        
        return base_distance + sub_distance

    @st.cache_data(ttl=3600)
    def get_coordinates(_self, address, area=None):
        """获取地址的坐标，使用缓存避免重复请求"""
        try:
            import re
            
            # 检查是否有特定邮政编码的精确坐标（移除641652让它使用普通geocoding）
            postal_coordinates = {
                '640526': (1.3486, 103.7065),  # Jurong West Street 61
                # '641652': 移除特殊坐标，让它使用普通geocoding获得正确位置
                '640652': (1.3500, 103.7070),  # Jurong West Street 65
                '640650': (1.3495, 103.7068),  # Jurong West Street 65 附近
                '640651': (1.3498, 103.7069),  # Jurong West Street 65 附近
                '641650': (1.3390, 103.7120),  # Jurong West Street 64 附近
                '641651': (1.3392, 103.7122),  # Jurong West Street 64 附近
                '641653': (1.3398, 103.7128),  # Jurong West Street 64 附近
            }
            
            # 从地址中提取邮政编码
            postal_match = re.search(r'Singapore\s+(\d{6})', address)
            if postal_match:
                postal_code = postal_match.group(1)
                if postal_code in postal_coordinates:
                    lat, lng = postal_coordinates[postal_code]
                    print(f"Using precise coordinates for postal code {postal_code}: {lat:.6f}, {lng:.6f}")
                    return lat, lng
            
            geolocator = Nominatim(user_agent="medical_search_app")
            
            # 清理地址：移除换行符和多余空格
            clean_address = address.replace('\n', ' ').replace('  ', ' ').strip()
            
            # 尝试1: 使用清理后的完整地址
            location = geolocator.geocode(f"{clean_address}", timeout=5)
            if location:
                print(f"Geocoded address: {clean_address} -> {location.latitude:.6f}, {location.longitude:.6f}")
                return location.latitude, location.longitude
            
            # 尝试2: 提取街道地址（去掉单元号）
            import re
            postal_match = re.search(r'(\d+\s+[\w\s]+Street\s+\d+)', clean_address)
            if postal_match:
                street_address = postal_match.group(1) + ', Singapore'
                time.sleep(0.5)
                location = geolocator.geocode(street_address, timeout=5)
                if location:
                    print(f"Geocoded street: {street_address} -> {location.latitude:.6f}, {location.longitude:.6f}")
                    return location.latitude, location.longitude
            
            # 尝试3: 如果有区域信息，使用区域名称
            if area:
                time.sleep(0.5)  # 避免API限制
                location = geolocator.geocode(f"{area}, Singapore", timeout=5)
                if location:
                    print(f"Geocoded area: {area} -> {location.latitude:.6f}, {location.longitude:.6f}")
                    return location.latitude, location.longitude
            
            # 尝试4: 使用更精确的区域坐标映射作为fallback
            if area:
                area_coords = {
                    'Jurong West': (1.347, 103.717),  # 更新为更准确的坐标
                    'Bedok': (1.324, 103.930),
                    'Tampines': (1.345, 103.944),
                    'Yishun': (1.429, 103.835),
                    'Woodlands': (1.437, 103.786),
                    'Ang Mo Kio': (1.375, 103.845),
                    'Sengkang': (1.391, 103.895),
                    'Punggol': (1.405, 103.902),
                    'Serangoon': (1.357, 103.874),
                    'Bukit Batok': (1.358, 103.754),
                    'Bukit Merah': (1.277, 103.823),
                    'Clementi': (1.315, 103.760),
                    'Hougang': (1.371, 103.886),
                    'Pasir Ris': (1.372, 103.949),
                    'Toa Payoh': (1.334, 103.856)
                }
                coords = area_coords.get(area)
                if coords:
                    print(f"Using fallback coordinates for {area}: {coords}")  # 调试信息
                return coords
                
        except Exception as e:
            print(f"Geocoding error for {address}: {e}")
        return None
    
    def create_map(self, clinic_results, query_postal=None):
        """创建显示诊所位置的交互式地图"""
        # 新加坡中心坐标
        singapore_center = [1.3521, 103.8198]
        
        # 创建地图
        m = folium.Map(
            location=singapore_center,
            zoom_start=11,
            tiles='OpenStreetMap'
        )
        
        # 如果有查询邮政编码，尝试添加查询位置标记
        if query_postal:
            # 更精确的邮政编码到坐标映射（与fallback坐标一致）
            postal_coords = {
                'Jurong West': [1.347, 103.717],  # 更新为更准确的坐标
                'Bedok': [1.324, 103.930],
                'Tampines': [1.345, 103.944],
                'Yishun': [1.429, 103.835],
                'Woodlands': [1.437, 103.786],
                'Ang Mo Kio': [1.375, 103.845],
                'Sengkang': [1.391, 103.895],
                'Punggol': [1.405, 103.902],
                'Serangoon': [1.357, 103.874],
                'Bukit Batok': [1.358, 103.754],
                'Pasir Ris': [1.372, 103.949]
            }
            
            # 直接获取查询邮政编码的精确坐标
            try:
                query_coords = self.get_coordinates(f"Singapore {query_postal}")
                if query_coords:
                    folium.Marker(
                        query_coords,
                        popup=f"📍 查询位置 (邮政编码: {query_postal})",
                        icon=folium.Icon(color='red', icon='search')
                    ).add_to(m)
                    print(f"Added query marker for postal code {query_postal} at {query_coords}")
                else:
                    # fallback: 根据最近的诊所推断查询位置
                    if clinic_results and len(clinic_results) > 0:
                        first_clinic_area = clinic_results[0].get('Area', '')
                        if first_clinic_area in postal_coords:
                            query_coords = postal_coords[first_clinic_area]
                            folium.Marker(
                                query_coords,
                                popup=f"📍 查询位置 (邮政编码: {query_postal})",
                                icon=folium.Icon(color='red', icon='search')
                            ).add_to(m)
                            print(f"Added fallback query marker for {query_postal} in {first_clinic_area}")
            except Exception as e:
                print(f"Error adding query location marker: {e}")
        
        # 添加诊所标记
        for i, clinic in enumerate(clinic_results[:10]):  # 最多显示10个诊所
            address = clinic.get('Address', '')
            name = clinic.get('Name', 'Unknown')
            area = clinic.get('Area', '')
            contact = clinic.get('Contact', clinic.get('Clinic Contact', ''))
            distance = clinic.get('_distance', '')
            
            # 定义区域fallback坐标
            area_fallback_coords = {
                'Bedok': (1.324, 103.930),
                'Tampines': (1.345, 103.944),
                'Jurong West': (1.347, 103.717),
                'Woodlands': (1.437, 103.786),
                'Yishun': (1.429, 103.835),
                'Ang Mo Kio': (1.375, 103.845),
                'Hougang': (1.361, 103.886),
                'Sengkang': (1.391, 103.895),
                'Punggol': (1.405, 103.902),
                'Serangoon': (1.357, 103.874),
                'Bukit Batok': (1.358, 103.754),
                'Pasir Ris': (1.372, 103.949),
                'Toa Payoh': (1.334, 103.848),
                'Bishan': (1.351, 103.848),
                'Kallang': (1.311, 103.862),
            }
            
            # 尝试获取精确坐标，fallback到区域坐标加小偏移
            coords = self.get_coordinates(address, area)
            
            if coords:
                coord_source = "Geocoded"
                print(f"Clinic {i+1} ({name}): Geocoded {coords} - {coord_source}")
            else:
                # 使用区域坐标但添加小偏移，让每个诊所显示在不同位置
                if area in area_fallback_coords:
                    base_lat, base_lng = area_fallback_coords[area]
                    # 添加小的随机偏移（0.001-0.005度，约100-500米）
                    import random
                    random.seed(hash(name) % 1000)  # 使用诊所名称作为种子，确保一致性
                    offset_lat = (random.random() - 0.5) * 0.01  # ±0.005度偏移
                    offset_lng = (random.random() - 0.5) * 0.01
                    coords = (base_lat + offset_lat, base_lng + offset_lng)
                    coord_source = f"Area-{area}-Offset"
                    print(f"Clinic {i+1} ({name}): Using area coordinates with offset {coords} - {coord_source}")
                else:
                    # 最后fallback到新加坡中心
                    coords = (1.3521, 103.8198)
                    coord_source = "Singapore-Center"
                    print(f"Clinic {i+1} ({name}): Using Singapore center {coords} - {coord_source}")
            
            # 确保总是有坐标
            if coords:
                lat, lng = coords
                
                # 创建弹出信息
                popup_html = f"""
                <div style='font-family: Arial, sans-serif; width: 250px;'>
                    <h4 style='margin: 0 0 10px 0; color: #2E8B57;'>🏥 {name}</h4>
                    <p style='margin: 5px 0;'><strong>📍 区域:</strong> {area}</p>
                    <p style='margin: 5px 0;'><strong>🏠 地址:</strong> {address}</p>
                    <p style='margin: 5px 0;'><strong>📞 电话:</strong> {contact}</p>
                    {f'<p style="margin: 5px 0;"><strong>📏 距离:</strong> {distance}</p>' if distance else ''}
                </div>
                """
                
                # 简化颜色判断逻辑
                if distance:
                    if distance <= 2000:  # 近距离
                        color = 'green'
                    else:  # 远距离
                        color = 'orange'
                else:
                    color = 'gray'  # 没有距离信息
                
                # 添加标记
                folium.Marker(
                    [lat, lng],
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=f"{i+1}. {name}",
                    icon=folium.Icon(color=color, icon='plus-sign')
                ).add_to(m)
                
                # 添加延迟避免API限制
                time.sleep(0.1)
        
        return m

def main():
    agent = MedicalAgent()

    with st.sidebar:
        st.header("⚙️ 设置")
        api_key = st.text_input("SiliconFlow API Key", type="password")
        if api_key:
            ok, msg = agent.connect_api(api_key, "https://api.siliconflow.cn/v1")
            if ok: st.success("✅ AI 已就绪")
            else: st.error(f"❌ 连接失败: {msg}")
       
        st.divider()
        st.info("💡 提示: 必须上传文件才能搜索")
        c_file = st.file_uploader("诊所数据 (Clinics)", type=['csv', 'xlsx'])
        d_file = st.file_uploader("医生数据 (Specialists)", type=['csv', 'xlsx'])

    st.title("🏥 新加坡医疗搜索 Agent")
    st.caption("架构: User Query -> LLM Intent Parsing -> Pandas Filtering -> Fuzzy Ranking")

    if c_file and d_file:
        df_c, df_d = agent.load_data(c_file, d_file)
        if df_c is not None and df_d is not None:
            st.success(f"📚 知识库加载完成: {len(df_d)} 位医生, {len(df_c)} 家诊所")
        else:
            st.error("❌ 数据文件加载失败，请检查文件格式是否正确")
       
        # 聊天交互区 - 只在数据加载成功时显示
        if df_c is not None and df_d is not None:
            if "history" not in st.session_state:
                st.session_state.history = []

            for q, r_list, plan in st.session_state.history:
                with st.chat_message("user"): st.write(q)
                with st.chat_message("assistant"):
                    # 展示思考过程
                    with st.expander("🧠 Agent 思考过程 (JSON)"):
                        st.json(plan)
                   
                    if not r_list:
                        st.warning("未找到匹配结果。")
                    else:
                        st.write(f"🔍 找到 {len(r_list)} 个结果:")
                        
                        # 检查是否为诊所搜索且有结果，显示地图
                        is_clinic_search = not (r_list and r_list[0].get('Specialty'))  # 没有Specialty字段说明是诊所
                        if is_clinic_search and len(r_list) > 0:
                            with st.expander("🗺️ 在地图上查看诊所位置", expanded=True):
                                # 获取查询邮政编码（如果有）
                                query_postal = plan.get('filters', {}).get('Area', '') if plan.get('filters', {}).get('Area', '').isdigit() else None
                                
                                # 创建并显示地图
                                with st.spinner("正在获取诊所坐标并生成地图..."):
                                    clinic_map = agent.create_map(r_list[:10], query_postal)
                                    
                                    # 添加简化图例
                                    legend_html = '''
                                    <div style="position: fixed; 
                                                top: 10px; right: 10px; width: 150px; height: auto; 
                                                background-color: white; border:2px solid grey; z-index:9999; 
                                                font-size:12px; padding: 8px">
                                    <h4 style="margin-top:0; margin-bottom:8px;">图例</h4>
                                    <p style="margin:3px 0;"><i class="fa fa-search" style="color:red"></i> 查询位置</p>
                                    <p style="margin:3px 0;"><i class="fa fa-circle" style="color:green"></i> 近距离</p>
                                    <p style="margin:3px 0;"><i class="fa fa-circle" style="color:orange"></i> 远距离</p>
                                    <p style="margin:3px 0;"><i class="fa fa-circle" style="color:gray"></i> 未知距离</p>
                                    </div>
                                    '''
                                    clinic_map.get_root().html.add_child(folium.Element(legend_html))
                                    
                                    st_folium(clinic_map, width=700, height=500)
                                
                                # 简化的地图说明
                                st.info("🗺️ **地图使用提示：** 点击任意标记查看诊所详细信息。右上角图例显示距离远近颜色说明。")
                        
                        for row in r_list:
                            # 智能判断是医生还是诊所数据
                            if 'Specialty' in row and row.get('Specialty'):
                                # 医生信息展示
                                name = row.get('Name', 'Unknown')
                                spec = row.get('Specialty', '')
                                lang = row.get('Languages', '')
                                svcs = row.get('Services', '')
                               
                                st.markdown(f"""
                                <div class="result-card">
                                    <div style="font-size:1.2em; font-weight:bold;">👨‍⚕️ {name}</div>
                                    <div class="tag-container">
                                        {f'<span class="tag tag-spec">{spec}</span>' if spec else ''}
                                        {f'<span class="tag tag-lang">🗣️ {lang}</span>' if lang else ''}
                                    </div>
                                    <div style="margin-top:10px; font-size:0.9em; color:#555;">
                                        {f'🛠️ <b>服务:</b> {svcs}' if svcs else ''}
                                    </div>
                                </div>
                                """, unsafe_allow_html=True)
                            else:
                                # 诊所信息展示
                                name = row.get('Name', 'Unknown')
                                area = row.get('Area', '')
                                address = row.get('Address', '')
                                contact = row.get('Contact', row.get('Clinic Contact', ''))
                                
                                # 格式化地址显示 - 彻底清理所有特殊字符
                                import re
                                if address:
                                    # 移除所有HTML标签
                                    address_clean = re.sub(r'<[^>]*>', '', address)
                                    # 移除换行符、制表符等特殊字符
                                    address_clean = re.sub(r'[\n\r\t]+', ' ', address_clean)
                                    # 合并多个空格
                                    address_clean = re.sub(r'\s+', ' ', address_clean)
                                    # HTML转义，防止特殊字符影响显示
                                    import html
                                    address_display = html.escape(address_clean.strip())
                                else:
                                    address_display = ''
                                
                                # 计算距离信息（使用预计算的距离）
                                distance_info = ''
                                if '_distance' in row and row['_distance'] is not None:
                                    distance = int(row['_distance'])
                                    distance_info = f'📏 <b>距离:</b> {distance} (邮政编码差值)<br>'
                                
                                st.markdown(f"""
<div class="result-card">
<div style="font-size:1.2em; font-weight:bold;">🏥 {name}</div>
<div class="tag-container">
{f'<span class="tag tag-loc">📍 {area}</span>' if area else ''}
</div>
<div style="margin-top:10px; font-size:0.9em; color:#555;">
{distance_info}
{f'🏠 <b>地址:</b> {address_display}' if address else ''}
{('<br>' if address and contact else '') + (f'📞 <b>电话:</b> {contact}' if contact else '')}
</div>
</div>
""", unsafe_allow_html=True)

            # 输入处理
            query = st.chat_input("请输入查询 (如: 'Find dr. Low Huey Moon', 或 'clinic nearest 179094')")
            if query:
                if not agent.client:
                    st.error("请先在左侧输入 API Key")
                else:
                    # 记录用户提问
                    st.session_state.history.append((query, [], {})) # 占位
                   
                    # 执行搜索
                    with st.spinner("小助手正在思考中..."):
                        results, plan = agent.search(df_c, df_d, query)
                       
                        # 更新历史记录
                        st.session_state.history[-1] = (query, results, plan)
                        st.rerun() # 刷新页面显示结果
        else:
            st.warning("请上传诊所和医生数据文件后再开始搜索")

if __name__ == "__main__":
    main()