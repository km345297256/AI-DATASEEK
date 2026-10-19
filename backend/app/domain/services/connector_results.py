"""Public, actionable projection of persisted connector search results."""
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.data_candidate import CandidateInput


def connector_results(event):
    if not isinstance(event, ToolEvent) or event.status != ToolStatus.CALLED:
        return None
    args = event.function_args or {}
    if event.function_name in ('ncbi_search_genomes', 'ncbi_describe_genome'):
        connector = 'ncbi-datasets-v2'
    elif event.function_name == 'data_connector_call' and args.get('operation') in ('search', 'describe'):
        connector = args.get('connector_id')
    else:
        return None
    titles = {'ncbi-datasets-v2': 'NCBI 基因组', 'ncbi-geo': 'NCBI GEO', 'ncbi-sra': 'NCBI SRA'}
    if connector not in titles:
        return None
    result = event.function_result
    result = result if isinstance(result, dict) else result.model_dump() if result else {}
    if result.get('success') is not True:
        return None
    data = result.get('data') or {}
    entries = data.get('resources', data.get('candidates'))
    if entries is None and data.get('external_id'):
        entries = [data]
    if not isinstance(entries, list):
        return None
    resources = []
    for entry in entries[:20]:
        try:
            public = CandidateInput.model_validate({key: entry[key] for key in ('title', 'source_url', 'external_id')})
        except (ValueError, KeyError, TypeError):
            continue
        resources.append(public.model_dump())
    return dict(connector_id=connector, title=titles[connector], resources=resources,
                can_download=True, total_count=data.get('total_count'))
