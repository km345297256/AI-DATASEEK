"""A file-preview shortcut must never swallow the user's analysis request."""
import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.message import Message
from app.domain.services.flows.plan_act import PlanActFlow
from test_dataset_fast_path import _dataset_with_files as _base_dataset


def _dataset_with_files(*paths):
    dataset = _base_dataset(*paths)
    for item in dataset.files:
        if item.path.endswith(".csv"):
            item.content_type = "text/csv"
    return dataset


@pytest.mark.parametrize('question', [
    '分析 observations.csv，计算 count、mean、min、max，输出 summary.csv 和 bar.png；图中完整显示六个标签及数值。',
    '使用 observations.csv 按 species 分组统计样本数和均值，只交付 summary.csv 与 scatter.png，显示三类图例及英文坐标轴标签。',
    '请先预览 observations.csv，再计算平均值并生成柱状图。',
    'Show observations.csv and calculate the mean of value.',
    'Plot observations.csv with all six labels displayed.',
    '不要预览 observations.csv，只计算均值。',
    '分析 observations.csv 的平均值，无需显示原始文件。',
    'Summarize observations.csv; do not open the raw file.',
])
@pytest.mark.parametrize('with_contract', [False, True])
def test_analysis_with_display_words_keeps_real_execution(question, with_contract):
    contract = [DeliverableRequirement(kind='table', formats=['csv'],
        output_paths=['/home/ubuntu/output/summary.csv'], objective='计算分组均值')] if with_contract else []
    message = Message(message=question, datasets=[_dataset_with_files('observations.csv')],
                      deliverables=contract, controller_target_files=['observations.csv'])
    step = PlanActFlow._create_dataset_fast_path_plan(message).steps[0]
    assert step.inputs['dataset_intent'] != 'file_preview'
    assert step.inputs['require_model_answer'] is True
    assert step.inputs['user_question'] == question
    assert step.deliverables == contract


@pytest.mark.parametrize('filename', ['open.csv', 'overview.csv', 'display.csv'])
def test_filename_is_never_a_preview_command(filename):
    step = PlanActFlow._create_dataset_fast_path_plan(Message(
        message=f'分析 {filename} 的数值分布', datasets=[_dataset_with_files(filename)])).steps[0]
    assert step.inputs['dataset_intent'] != 'file_preview'


@pytest.mark.parametrize('question,path', [
    ('请预览 REPORT.PDF', 'documents/Report.Pdf'),
    ('Open MAP.PNG', 'figures/Map.png'),
    ('显示 notes/readme.MD', 'notes/Readme.md'),
    ('可视化下这个文件 image.jpg', 'photos/image.jpg'),
    ('Please show the contents of `observations.csv`.', 'observations.csv'),
    ('帮我打开一下 observations.csv，谢谢。', 'observations.csv'),
])
def test_narrow_original_file_preview_is_preserved(question, path):
    step = PlanActFlow._create_dataset_fast_path_plan(Message(
        message=question, datasets=[_dataset_with_files(path)])).steps[0]
    assert step.inputs['dataset_intent'] == 'file_preview'


def test_explicit_output_contract_cannot_be_bypassed_by_preview_wording():
    request = DeliverableRequirement(kind='image', formats=['png'],
        output_paths=['/home/ubuntu/output/derived.png'], objective='绘制各组均值')
    step = PlanActFlow._create_dataset_fast_path_plan(Message(message='显示 observations.csv',
        datasets=[_dataset_with_files('observations.csv')], deliverables=[request])).steps[0]
    assert step.inputs['dataset_intent'] != 'file_preview'
    assert step.deliverables == [request]


def test_repeated_display_phrases_with_trailing_analysis_still_fall_back():
    # Overlapping neutral/action phrases must not hang routing on long requests.
    question = '显示原图' * 1000 + ' observations.csv 计算均值'
    step = PlanActFlow._create_dataset_fast_path_plan(Message(message=question,
        datasets=[_dataset_with_files('observations.csv')])).steps[0]
    assert step.inputs['dataset_intent'] != 'file_preview'
