import type { FileInfo } from '../api/file';

const LEGACY_STORAGE_KEY = 'ai-dataseek:pending-chat';
const STORAGE_KEY_PREFIX = 'ai-dataseek:pending-chat:';

export interface PendingChat {
    sessionId: string;
    message: string;
    files: FileInfo[];
    skills: string[];
    mcpServers: string[];
    agentProfileId?: string | null;
}

export const savePendingChat = (pendingChat: PendingChat) => {
    sessionStorage.setItem(`${STORAGE_KEY_PREFIX}${pendingChat.sessionId}`, JSON.stringify(pendingChat));
};

export const consumePendingChat = (sessionId: string): PendingChat | null => {
    const storageKey = `${STORAGE_KEY_PREFIX}${sessionId}`;
    const raw = sessionStorage.getItem(storageKey) ?? sessionStorage.getItem(LEGACY_STORAGE_KEY);
    if (!raw) return null;

    try {
        const pendingChat = JSON.parse(raw) as PendingChat;
        if (pendingChat.sessionId !== sessionId) return null;
        sessionStorage.removeItem(storageKey);
        sessionStorage.removeItem(LEGACY_STORAGE_KEY);
        return pendingChat;
    } catch {
        sessionStorage.removeItem(storageKey);
        sessionStorage.removeItem(LEGACY_STORAGE_KEY);
        return null;
    }
};
