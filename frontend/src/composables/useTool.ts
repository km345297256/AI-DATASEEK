import { computed, Ref } from 'vue';
import type { ToolContent } from '../types/message';
import { useI18n } from 'vue-i18n';
import { TOOL_ICON_MAP, TOOL_NAME_MAP, TOOL_FUNCTION_MAP, TOOL_FUNCTION_CALLED_MAP, TOOL_COMPONENT_MAP } from '../constants/tool';
import { getToolDisplayDetail, resolveToolFunction, resolveToolName } from '../utils/toolDisplay';

export function useToolInfo(tool?: Ref<ToolContent | undefined>) {
  const { t } = useI18n();

  const toolInfo = computed(() => {
    if (!tool || !tool.value) return null;
    const toolName = resolveToolName(tool.value);
    const functionName = resolveToolFunction(tool.value);

    // MCP tool
    if (functionName.startsWith('mcp_')) {
      const mcpToolName = functionName.replace(/^mcp_/, '');
      const detail = getToolDisplayDetail(tool.value);
      
      return {
        icon: TOOL_ICON_MAP['mcp'] || null,
        name: t(TOOL_NAME_MAP['mcp'] || 'MCP Tool'),
        function: mcpToolName || t('MCP Tool'),
        functionArg: detail.full,
        functionArgPreview: detail.preview,
        functionArgExpandable: detail.expandable,
        view: TOOL_COMPONENT_MAP['mcp'] || null
      };
    }
    
    const detail = getToolDisplayDetail(tool.value);
    const labelMap = tool.value.status === 'called' ? TOOL_FUNCTION_CALLED_MAP : TOOL_FUNCTION_MAP;
    const localizedFunction = functionName ? t(labelMap[functionName] || TOOL_FUNCTION_MAP[functionName] || functionName) : t('Unknown Tool');
    
    return {
      icon: TOOL_ICON_MAP[toolName] || null,
      name: t(TOOL_NAME_MAP[toolName] || toolName || 'Tool'),
      function: localizedFunction,
      functionArg: detail.full,
      functionArgPreview: detail.preview,
      functionArgExpandable: detail.expandable,
      view: TOOL_COMPONENT_MAP[toolName] || null
    };
  });

  return {
    toolInfo
  };
} 
