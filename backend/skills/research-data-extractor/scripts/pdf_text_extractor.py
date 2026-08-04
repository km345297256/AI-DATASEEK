#!/usr/bin/env python3
"""
PDF文本抽取工具
从《国家重点研发项目任务书》PDF中按章节抽取文本内容
"""

import fitz  # PyMuPDF
import re
from pathlib import Path


class PDFTextExtractor:
    """PDF文本抽取器"""
    
    # 章节标识模式
    SECTION_PATTERNS = {
        'zhibiao_start': [
            r'一、\s*项目',
            r'一、\s*课题',
            r'一、\s*考核',
            r'\(一\)\s*项目',
            r'\(一\)\s*课题',
            r'1\.\s*项目',
            r'1\.\s*课题'
        ],
        'zhibiao_end': [
            r'备注\s*[:：]',
            r'附注\s*[:：]',
            r'注意\s*[:：]',
            r'\n\s*备注',
            r'\n\s*附注'
        ],
        'keti_start': [
            r'二、\s*项目',
            r'二、\s*课题',
            r'二、\s*研究内容',
            r'\(二\)\s*项目',
            r'\(二\)\s*课题',
            r'2\.\s*项目',
            r'2\.\s*课题',
            r'2\.\s*研究内容'
        ],
        'keti_end': [
            r'五、\s*预期',
            r'五、\s*预期成果',
            r'五、\s*预期目标',
            r'五、\s*预期产出',
            r'\(五\)\s*预期',
            r'5\.\s*预期'
        ]
    }
    
    def __init__(self, pdf_path: str):
        self.pdf_path = Path(pdf_path)
        self.doc = fitz.open(str(pdf_path))
        self.full_text = self._extract_full_text()
        
    def _extract_full_text(self) -> str:
        """提取PDF全部文本"""
        text = ""
        for page_num in range(len(self.doc)):
            page = self.doc.load_page(page_num)
            text += page.get_text()
        return text
    
    def _find_section_positions(self, patterns: list) -> list:
        """查找章节位置"""
        positions = []
        for pattern in patterns:
            matches = list(re.finditer(pattern, self.full_text))
            positions.extend(matches)
        return sorted(positions, key=lambda m: m.start())
    
    def extract_zhibiao(self) -> str:
        """
        提取考核指标部分
        起始："一、项目"章节
        结束："备注："或"附注："
        """
        start_positions = self._find_section_positions(self.SECTION_PATTERNS['zhibiao_start'])
        end_positions = self._find_section_positions(self.SECTION_PATTERNS['zhibiao_end'])
        
        if not start_positions:
            print("警告：未找到考核指标起始标识（一、项目/课题）")
            return ""
        
        start_pos = start_positions[0].start()
        
        # 找到第一个在start_pos之后的end_pos
        end_pos = None
        for end_match in end_positions:
            if end_match.start() > start_pos:
                end_pos = end_match.start()
                break
        
        if end_pos is None:
            print("警告：未找到考核指标结束标识（备注/附注），将截取到文档末尾")
            end_pos = len(self.full_text)
        
        extracted_text = self.full_text[start_pos:end_pos]
        
        # 清理文本
        extracted_text = self._clean_text(extracted_text)
        
        return extracted_text
    
    def extract_keti(self) -> str:
        """
        提取课题研究内容部分
        起始："二、项目"或"二、课题"章节
        结束："五、预期"章节
        """
        start_positions = self._find_section_positions(self.SECTION_PATTERNS['keti_start'])
        end_positions = self._find_section_positions(self.SECTION_PATTERNS['keti_end'])
        
        if not start_positions:
            print("警告：未找到课题研究内容起始标识（二、项目/课题）")
            return ""
        
        start_pos = start_positions[0].start()
        
        # 找到第一个在start_pos之后的end_pos
        end_pos = None
        for end_match in end_positions:
            if end_match.start() > start_pos:
                end_pos = end_match.start()
                break
        
        if end_pos is None:
            print("警告：未找到课题研究内容结束标识（五、预期），将截取到文档末尾")
            end_pos = len(self.full_text)
        
        extracted_text = self.full_text[start_pos:end_pos]
        
        # 清理文本
        extracted_text = self._clean_text(extracted_text)
        
        return extracted_text
    
    def _clean_text(self, text: str) -> str:
        """清理文本"""
        # 移除页眉页脚常见的页码格式
        text = re.sub(r'\n\s*\d+\s*\n', '\n', text)  # 单独一行的数字
        text = re.sub(r'\n\s*-\s*\d+\s*-\s*\n', '\n', text)  # -数字- 格式
        
        # 移除多余的空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # 移除行首行尾的空白字符
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(lines)
        
        return text.strip()
    
    def save_to_file(self, content: str, output_path: str):
        """保存内容到文件"""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"已保存: {output_path}")
    
    def close(self):
        """关闭PDF文档"""
        self.doc.close()


def main():
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='从项目任务书PDF中提取文本')
    parser.add_argument('pdf_path', help='PDF文件路径')
    parser.add_argument('output_dir', help='输出目录')
    parser.add_argument('--project-name', '-n', help='项目名称（用于命名输出文件）')
    
    args = parser.parse_args()
    
    # 确定项目名称
    if args.project_name:
        project_name = args.project_name
    else:
        project_name = Path(args.pdf_path).stem
    
    # 创建抽取器
    extractor = PDFTextExtractor(args.pdf_path)
    
    try:
        # 抽取考核指标
        print("正在抽取考核指标...")
        zhibiao_text = extractor.extract_zhibiao()
        if zhibiao_text:
            zhibiao_path = Path(args.output_dir) / f"{project_name}_考核指标.txt"
            extractor.save_to_file(zhibiao_text, str(zhibiao_path))
        
        # 抽取课题研究内容
        print("正在抽取课题研究内容...")
        keti_text = extractor.extract_keti()
        if keti_text:
            keti_path = Path(args.output_dir) / f"{project_name}_课题研究内容.txt"
            extractor.save_to_file(keti_text, str(keti_path))
        
        print("\n抽取完成！")
        
    finally:
        extractor.close()


if __name__ == "__main__":
    main()
