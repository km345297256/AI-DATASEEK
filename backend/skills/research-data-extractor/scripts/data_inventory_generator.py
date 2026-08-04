#!/usr/bin/env python3
"""
科学数据资源清单生成器
根据考核指标和研究内容自动生成数据清单
"""

import re
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


class DataInventoryGenerator:
    """数据清单生成器"""
    
    # 数据类型映射
    DATA_TYPE_MAP = {
        '实验': '原始实验数据',
        '测试': '原始实验数据',
        '检测': '原始实验数据',
        '分析': '原始实验数据',
        '观测': '观测监测数据',
        '监测': '观测监测数据',
        '调查': '调查统计数据',
        '问卷': '调查统计数据',
        '统计': '调查统计数据',
        '仿真': '仿真模拟数据',
        '模拟': '仿真模拟数据',
        '计算': '仿真模拟数据',
        '模型': '仿真模拟数据',
        '软件': '软件工具',
        '系统': '软件工具',
        '平台': '软件工具',
        '文献': '文献资料数据',
        '论文': '文献资料数据',
        '专利': '文献资料数据',
        '标准': '标准规范',
        '规范': '标准规范',
        '规范': '标准规范'
    }
    
    # 数据格式映射
    FORMAT_MAP = {
        '原始实验数据': ['.xlsx', '.csv', '.txt'],
        '观测监测数据': ['.nc', '.hdf', '.csv', '.txt'],
        '调查统计数据': ['.xlsx', '.csv', '.sav', '.dta'],
        '仿真模拟数据': ['.nc', '.hdf5', '.mat', '.vtk'],
        '文献资料数据': ['.pdf', '.docx', '.txt'],
        '软件工具': ['.zip', '.tar.gz', '.py', '.m', '.jar'],
        '标准规范': ['.pdf', '.docx'],
        '其他': ['.zip', '.rar']
    }
    
    def __init__(self, zhibiao_text: str, keti_text: str):
        self.zhibiao_text = zhibiao_text
        self.keti_text = keti_text
        self.data_items: List[Dict[str, Any]] = []
        
    def extract_quantitative_indicators(self) -> List[Dict]:
        """从考核指标中提取量化指标"""
        indicators = []
        
        # 匹配模式：数字+单位
        patterns = [
            r'(完成|采集|收集|获取|分析|测试|制备|研制|开发|建立|形成|发表|申请)\s*不少于\s*(\d+)\s*(个|套|种|篇|项|份|组|台|例|件|GB|TB|MB|年|月|日)?',
            r'(完成|采集|收集|获取|分析|测试|制备|研制|开发|建立|形成|发表|申请)\s*(\d+)\s*(-|~|至)\s*(\d+)\s*(个|套|种|篇|项|份|组|台|例|件|GB|TB|MB|年|月|日)?',
            r'(\d+)\s*(个|套|种|篇|项|份|组|台|例|件)\s*(样本|数据|记录|报告|论文|专利|软件|标准)',
            r'(发表|申请|授权)\s*SCI\s*论文\s*(\d+)\s*篇',
            r'(申请|授权|获得)\s*(发明专利|实用新型|软件著作权)\s*(\d+)\s*项',
        ]
        
        for pattern in patterns:
            matches = re.finditer(pattern, self.zhibiao_text)
            for match in matches:
                indicators.append({
                    'action': match.group(1),
                    'quantity': match.group(2),
                    'unit': match.group(3) if len(match.groups()) > 2 else '个'
                })
        
        return indicators
    
    def extract_research_activities(self) -> List[Dict]:
        """从研究内容中提取研究活动"""
        activities = []
        
        # 识别研究活动关键词
        activity_patterns = [
            r'开展(.*?)(研究|实验|测试|分析|调查|观测|监测|仿真|模拟)',
            r'进行(.*?)(研究|实验|测试|分析|调查|观测|监测|仿真|模拟)',
            r'建立(.*?)(模型|系统|平台|数据库|方法)',
            r'开发(.*?)(软件|系统|平台|工具)',
            r'研制(.*?)(设备|装置|样机|产品)',
            r'构建(.*?)(模型|系统|平台|数据库)',
            r'采集(.*?)(数据|样本|信息)',
            r'收集(.*?)(数据|样本|资料)',
        ]
        
        for pattern in activity_patterns:
            matches = re.finditer(pattern, self.keti_text)
            for match in matches:
                activities.append({
                    'action': match.group(0)[:10],
                    'target': match.group(1),
                    'type': match.group(2)
                })
        
        return activities
    
    def determine_data_type(self, activity: Dict) -> str:
        """根据研究活动确定数据类型"""
        activity_text = f"{activity.get('action', '')} {activity.get('type', '')}"
        
        for keyword, data_type in self.DATA_TYPE_MAP.items():
            if keyword in activity_text:
                return data_type
        
        return '其他'
    
    def generate_data_items(self) -> List[Dict]:
        """生成数据清单条目"""
        indicators = self.extract_quantitative_indicators()
        activities = self.extract_research_activities()
        
        data_items = []
        item_no = 1
        
        # 基于研究活动生成数据条目
        for i, activity in enumerate(activities):
            data_type = self.determine_data_type(activity)
            formats = self.FORMAT_MAP.get(data_type, ['.txt'])
            
            # 查找相关的量化指标
            quantity = "待定"
            if i < len(indicators):
                quantity = f"{indicators[i].get('quantity', '待定')} {indicators[i].get('unit', '个')}"
            
            item = {
                '序号': f'{item_no:03d}',
                '数据资源名称': f"{activity.get('target', '研究数据')}_{data_type}",
                '数据类型': data_type,
                '数据格式': ', '.join(formats),
                '数据量': quantity,
                '产生阶段': '项目执行期',
                '汇交方式': '在线汇交',
                '开放属性': '受控开放',
                '备注': f"来源于：{activity.get('action', '')}"
            }
            
            data_items.append(item)
            item_no += 1
        
        # 基于考核指标生成成果类数据条目
        output_patterns = [
            (r'发表.*?论文\s*(\d+)\s*篇', '论文成果', '文献资料数据', '.pdf, .docx'),
            (r'申请.*?专利\s*(\d+)\s*项', '专利成果', '文献资料数据', '.pdf'),
            (r'获得.*?软件著作权\s*(\d+)\s*项', '软件著作权', '软件工具', '.zip, .pdf'),
            (r'制定.*?标准\s*(\d+)\s*项', '标准规范', '标准规范', '.pdf, .docx'),
        ]
        
        for pattern, name, data_type, formats in output_patterns:
            matches = re.finditer(pattern, self.zhibiao_text)
            for match in matches:
                quantity = match.group(1) if match.groups() else "待定"
                
                item = {
                    '序号': f'{item_no:03d}',
                    '数据资源名称': name,
                    '数据类型': data_type,
                    '数据格式': formats,
                    '数据量': f'{quantity} 项',
                    '产生阶段': '项目结题前',
                    '汇交方式': '在线汇交',
                    '开放属性': '完全开放' if '论文' in name else '受控开放',
                    '备注': '考核指标要求'
                }
                
                data_items.append(item)
                item_no += 1
        
        self.data_items = data_items
        return data_items
    
    def generate_markdown(self, project_info: Dict = None) -> str:
        """生成Markdown格式的数据清单"""
        if not self.data_items:
            self.generate_data_items()
        
        if project_info is None:
            project_info = {
                'PROJECT_NAME': '待填写',
                'PROJECT_CODE': '待填写',
                'ORGANIZATION': '待填写',
                'PRINCIPAL_INVESTIGATOR': '待填写',
                'EXECUTION_PERIOD': '待填写',
                'PROGRAM': '待填写',
                'FIELD': '待填写'
            }
        
        # 按数据类型分组
        grouped_items = {}
        for item in self.data_items:
            data_type = item['数据类型']
            if data_type not in grouped_items:
                grouped_items[data_type] = []
            grouped_items[data_type].append(item)
        
        # 生成Markdown
        md_lines = []
        md_lines.append('# 科学数据资源清单')
        md_lines.append('')
        md_lines.append('## 项目基本信息')
        md_lines.append('')
        md_lines.append('| 信息项 | 内容 |')
        md_lines.append('|-------|-----|')
        md_lines.append(f"| 项目名称 | {project_info.get('PROJECT_NAME', '待填写')} |")
        md_lines.append(f"| 项目编号 | {project_info.get('PROJECT_CODE', '待填写')} |")
        md_lines.append(f"| 承担单位 | {project_info.get('ORGANIZATION', '待填写')} |")
        md_lines.append(f"| 项目负责人 | {project_info.get('PRINCIPAL_INVESTIGATOR', '待填写')} |")
        md_lines.append(f"| 执行周期 | {project_info.get('EXECUTION_PERIOD', '待填写')} |")
        md_lines.append('')
        md_lines.append('---')
        md_lines.append('')
        md_lines.append('## 数据资源清单')
        md_lines.append('')
        
        # 按数据类型输出表格
        data_type_names = {
            '原始实验数据': '一、原始实验数据',
            '观测监测数据': '二、观测监测数据',
            '调查统计数据': '三、调查统计数据',
            '仿真模拟数据': '四、仿真模拟数据',
            '文献资料数据': '五、文献资料数据',
            '软件工具': '六、软件工具',
            '标准规范': '七、标准规范',
            '其他': '八、其他数据'
        }
        
        for data_type, title in data_type_names.items():
            if data_type in grouped_items:
                md_lines.append(f"### {title}")
                md_lines.append('')
                md_lines.append('| 序号 | 数据资源名称 | 数据类型 | 数据格式 | 数据量 | 产生阶段 | 汇交方式 | 开放属性 | 备注 |')
                md_lines.append('|-----|------------|---------|---------|-------|---------|---------|---------|-----|')
                
                for item in grouped_items[data_type]:
                    md_lines.append(f"| {item['序号']} | {item['数据资源名称']} | {item['数据类型']} | {item['数据格式']} | {item['数据量']} | {item['产生阶段']} | {item['汇交方式']} | {item['开放属性']} | {item['备注']} |")
                
                md_lines.append('')
        
        # 添加统计信息
        md_lines.append('---')
        md_lines.append('')
        md_lines.append('## 数据统计')
        md_lines.append('')
        md_lines.append(f"**数据资源总数**: {len(self.data_items)} 项")
        md_lines.append('')
        md_lines.append('### 按数据类型统计')
        md_lines.append('')
        md_lines.append('| 数据类型 | 条目数 |')
        md_lines.append('|---------|-------|')
        for data_type, items in grouped_items.items():
            md_lines.append(f"| {data_type} | {len(items)} |")
        md_lines.append('')
        
        # 添加生成说明
        md_lines.append('---')
        md_lines.append('')
        md_lines.append('## 数据生成说明')
        md_lines.append('')
        md_lines.append('### 数据产生逻辑')
        md_lines.append('')
        md_lines.append('本数据清单基于以下逻辑生成：')
        md_lines.append('')
        md_lines.append('1. **研究活动推导**：根据课题研究内容中描述的研究活动，推导其产生的数据类型')
        md_lines.append('2. **考核指标映射**：将考核指标中的量化要求映射为具体的数据条目')
        md_lines.append('3. **成果形式识别**：识别考核指标中要求的论文、专利、软件等成果形式')
        md_lines.append('')
        md_lines.append('### 数据质量控制')
        md_lines.append('')
        md_lines.append('- 原始数据需包含完整的元数据信息')
        md_lines.append('- 实验数据需记录实验条件、仪器参数等信息')
        md_lines.append('- 数据格式应符合相关领域标准规范')
        md_lines.append('- 数据汇交前需进行质量检查和校验')
        md_lines.append('')
        md_lines.append('### 数据共享策略')
        md_lines.append('')
        md_lines.append('- 涉密数据按照国家保密法规进行管理')
        md_lines.append('- 涉及个人隐私的数据需进行脱敏处理')
        md_lines.append('- 合作单位共享数据需签订数据共享协议')
        md_lines.append('- 公开发表的数据应遵循FAIR原则')
        md_lines.append('')
        
        # 添加页脚
        md_lines.append('---')
        md_lines.append('')
        md_lines.append(f"**生成日期**: {datetime.now().strftime('%Y年%m月%d日')}")
        md_lines.append('')
        md_lines.append('*本清单由科研数据深度抽取专家系统自动生成，具体内容需根据项目实际情况核实完善。*')
        
        return '\n'.join(md_lines)
    
    def save(self, output_path: str, project_info: Dict = None):
        """保存数据清单到文件"""
        markdown = self.generate_markdown(project_info)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(markdown)
        print(f"数据清单已保存: {output_path}")


def main():
    import sys
    
    if len(sys.argv) < 4:
        print("用法: python data_inventory_generator.py <考核指标文件> <课题研究内容文件> <输出文件路径>")
        print("示例: python data_inventory_generator.py zhibiao.txt keti.txt output/数据资源清单.md")
        sys.exit(1)
    
    zhibiao_path = sys.argv[1]
    keti_path = sys.argv[2]
    output_path = sys.argv[3]
    
    # 读取输入文件
    with open(zhibiao_path, 'r', encoding='utf-8') as f:
        zhibiao_text = f.read()
    
    with open(keti_path, 'r', encoding='utf-8') as f:
        keti_text = f.read()
    
    # 生成数据清单
    generator = DataInventoryGenerator(zhibiao_text, keti_text)
    generator.save(output_path)
    
    print("数据清单生成完成！")


if __name__ == "__main__":
    main()
