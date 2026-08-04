import { computed, Ref } from 'vue';
import type { ToolContent } from '../types/message';
import { useI18n } from 'vue-i18n';
import { TOOL_ICON_MAP, TOOL_NAME_MAP, TOOL_FUNCTION_MAP, TOOL_FUNCTION_ARG_MAP, TOOL_COMPONENT_MAP } from '../constants/tool';

export function useToolInfo(tool?: Ref<ToolContent | undefined>) {
  const { t } = useI18n();

  const toolInfo = computed(() => {
    if (!tool || !tool.value) return null;
    const toolName = tool.value.name || '';
    const functionName = tool.value.function || '';

    // MCP tool
    if (functionName.startsWith('mcp_')) {
      const mcpToolName = functionName.replace(/^mcp_/, '');
      let functionArg = '';
      
      const args = tool.value.args;
      if (args && Object.keys(args).length > 0) {
        const firstKey = Object.keys(args)[0];
        const firstValue = args[firstKey];
        if (typeof firstValue === 'string' && firstValue.length < 50) {
          functionArg = firstValue;
        } else if (firstValue !== undefined) {
          functionArg = JSON.stringify(firstValue).substring(0, 30) + '...';
        }
      }
      
      return {
        icon: TOOL_ICON_MAP['mcp'] || null,
        name: t(TOOL_NAME_MAP['mcp'] || 'MCP Tool'),
        function: mcpToolName || t('MCP Tool'),
        functionArg: functionArg,
        view: TOOL_COMPONENT_MAP['mcp'] || null
      };
    }
    
    const args = tool.value.args || {};
    const argKey = TOOL_FUNCTION_ARG_MAP[functionName];
    let functionArg = argKey ? args[argKey] || '' : '';
    if (argKey === 'file') {
      functionArg = functionArg.replace(/^\/home\/ubuntu\//, '');
    }
    const localizedFunction = functionName ? t(TOOL_FUNCTION_MAP[functionName] || functionName) : t('Unknown Tool');
    
    return {
      icon: TOOL_ICON_MAP[toolName] || null,
      name: t(TOOL_NAME_MAP[toolName] || toolName || 'Tool'),
      function: localizedFunction,
      functionArg: functionArg || '',
      view: TOOL_COMPONENT_MAP[toolName] || null
    };
  });

  return {
    toolInfo
  };
} 
