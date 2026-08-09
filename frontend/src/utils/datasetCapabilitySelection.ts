export const DATASET_CHAT_PLACEHOLDER = '针对当前数据集提问，输入 / 使用技能...';

export interface DatasetChatCapabilities {
  attachments: [];
  skills: string[];
  mcpServers: string[];
  datasetIds: string[];
}

export function buildDatasetChatCapabilities(
  datasetId: string,
  skills: string[],
  mcpServers: string[] = [],
): DatasetChatCapabilities {
  return {
    attachments: [],
    skills: [...skills],
    mcpServers: [...mcpServers],
    datasetIds: datasetId ? [datasetId] : [],
  };
}
