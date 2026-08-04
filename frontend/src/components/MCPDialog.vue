<template>
  <Dialog v-model:open="open">
    <DialogContent class="w-[640px]">
      <DialogHeader>
        <DialogTitle>{{ t('MCP Tools') }}</DialogTitle>
      </DialogHeader>

      <div class="px-6 pb-5 pt-2">
        <div class="mb-4 flex items-center justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-[var(--text-secondary)]">{{ t('Select MCP servers for this message') }}</p>
            <p class="mt-1 text-xs text-[var(--text-tertiary)]">{{ t('Add stdio, SSE, or streamable HTTP MCP servers') }}</p>
          </div>
          <button
            class="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[var(--border-btn-main)] px-3 text-sm text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
            @click="startCreate"
          >
            <Plus :size="15" />
            <span>{{ t('Add') }}</span>
          </button>
        </div>

        <div v-if="editing" class="mb-4 rounded-lg border border-[var(--border-main)] p-3">
          <div class="grid grid-cols-2 gap-3">
            <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              {{ t('Name') }}
              <input
                v-model="form.name"
                :disabled="editingExisting"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none"
              />
            </label>
            <label class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              {{ t('Transport') }}
              <select
                v-model="form.transport"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] outline-none"
              >
                <option value="stdio">stdio</option>
                <option value="sse">sse</option>
                <option value="streamable-http">streamable-http</option>
              </select>
            </label>
            <label class="col-span-2 flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              {{ t('Description') }}
              <input
                v-model="form.description"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none"
              />
            </label>
            <label v-if="form.transport === 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              {{ t('Command') }}
              <input
                v-model="form.command"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none"
              />
            </label>
            <label v-if="form.transport === 'stdio'" class="flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              {{ t('Args') }}
              <input
                v-model="argsText"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none"
              />
            </label>
            <label v-if="form.transport !== 'stdio'" class="col-span-2 flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              URL
              <input
                v-model="form.url"
                class="h-9 rounded-lg border border-[var(--border-main)] bg-transparent px-2 text-sm text-[var(--text-primary)] outline-none"
              />
            </label>
            <label v-if="form.transport !== 'stdio'" class="col-span-2 flex flex-col gap-1 text-xs text-[var(--text-secondary)]">
              Headers JSON
              <textarea
                v-model="headersText"
                class="min-h-[84px] rounded-lg border border-[var(--border-main)] bg-transparent px-2 py-2 font-mono text-xs text-[var(--text-primary)] outline-none"
                placeholder='{"Authorization":"Bearer token","X-API-Key":"key"}'
              ></textarea>
            </label>
            <label class="col-span-2 flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input v-model="form.enabled" type="checkbox" class="h-4 w-4 accent-[var(--Button-primary-black)]" />
              {{ t('Enabled') }}
            </label>
          </div>
          <div class="mt-3 flex justify-end gap-2">
            <button
              class="rounded-[10px] border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-dark)]"
              @click="editing = false"
            >
              {{ t('Cancel') }}
            </button>
            <button
              class="rounded-[10px] bg-[var(--Button-primary-black)] px-3 py-2 text-sm text-[var(--text-onblack)] hover:opacity-90"
              :disabled="saving"
              @click="handleSave"
            >
              {{ saving ? t('Saving...') : t('Save') }}
            </button>
          </div>
        </div>

        <div v-if="loading" class="py-8 text-center text-sm text-[var(--text-tertiary)]">
          {{ t('Loading') }}...
        </div>
        <div v-else-if="servers.length === 0" class="rounded-lg border border-dashed border-[var(--border-main)] py-8 text-center text-sm text-[var(--text-tertiary)]">
          {{ t('No MCP servers yet') }}
        </div>
        <div v-else class="max-h-[320px] overflow-y-auto rounded-lg border border-[var(--border-main)]">
          <label
            v-for="server in servers"
            :key="server.name"
            class="flex cursor-pointer items-start gap-3 border-b border-[var(--border-main)] px-3 py-3 last:border-b-0 hover:bg-[var(--fill-tsp-white-light)]"
          >
            <input
              type="checkbox"
              class="mt-1 h-4 w-4 accent-[var(--Button-primary-black)]"
              :disabled="!server.enabled"
              :checked="selectedServers.includes(server.name)"
              @change="toggleServer(server.name)"
            />
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <div class="truncate text-sm font-medium text-[var(--text-primary)]">{{ server.name }}</div>
                <span class="rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[11px] text-[var(--text-tertiary)]">{{ server.transport }}</span>
                <span v-if="!server.enabled" class="text-[11px] text-[var(--text-tertiary)]">{{ t('Disabled') }}</span>
              </div>
              <div class="mt-1 text-xs leading-5 text-[var(--text-tertiary)]">
                {{ server.description || server.url || server.command || t('No description') }}
              </div>
            </div>
            <div class="flex shrink-0 gap-1">
              <button class="rounded-md p-1 hover:bg-[var(--fill-tsp-white-dark)]" type="button" @click.prevent="startEdit(server)">
                <Pencil :size="14" />
              </button>
              <button class="rounded-md p-1 hover:bg-[var(--fill-tsp-white-dark)]" type="button" @click.prevent="handleDelete(server.name)">
                <Trash2 :size="14" />
              </button>
            </div>
          </label>
        </div>

        <DialogFooter>
          <button
            class="rounded-[10px] border border-[var(--border-btn-main)] px-3 py-2 text-sm text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-dark)]"
            @click="open = false"
          >
            {{ t('Done') }}
          </button>
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { Plus, Pencil, Trash2 } from 'lucide-vue-next';
import { useI18n } from 'vue-i18n';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { deleteMCPServer, listMCPServers, saveMCPServer, type MCPServerInfo, type MCPTransport } from '@/api/mcp';
import { showErrorToast, showSuccessToast } from '@/utils/toast';

const props = defineProps<{
  open: boolean;
  selectedServers: string[];
}>();

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void;
  (e: 'update:selectedServers', value: string[]): void;
}>();

const { t } = useI18n();
const servers = ref<MCPServerInfo[]>([]);
const loading = ref(false);
const saving = ref(false);
const editing = ref(false);
const editingExisting = ref(false);
const argsText = ref('');
const headersText = ref('');

const form = reactive<MCPServerInfo>({
  name: '',
  transport: 'stdio',
  enabled: true,
  description: '',
  command: '',
  args: [],
  url: '',
});

const open = computed({
  get: () => props.open,
  set: (value: boolean) => emit('update:open', value),
});

const selectedServers = computed(() => props.selectedServers ?? []);

const resetForm = () => {
  form.name = '';
  form.transport = 'stdio';
  form.enabled = true;
  form.description = '';
  form.command = '';
  form.args = [];
  form.url = '';
  form.headers = undefined;
  form.env = undefined;
  argsText.value = '';
  headersText.value = '';
};

const loadServers = async () => {
  loading.value = true;
  try {
    servers.value = await listMCPServers();
  } catch (error) {
    console.error('Failed to load MCP servers:', error);
    showErrorToast(t('Failed to load MCP servers'));
  } finally {
    loading.value = false;
  }
};

const startCreate = () => {
  resetForm();
  editingExisting.value = false;
  editing.value = true;
};

const startEdit = (server: MCPServerInfo) => {
  form.name = server.name;
  form.transport = server.transport;
  form.enabled = server.enabled;
  form.description = server.description || '';
  form.command = server.command || '';
  form.args = server.args || [];
  form.url = server.url || '';
  form.headers = server.headers;
  form.env = server.env;
  argsText.value = (server.args || []).join(' ');
  headersText.value = server.headers ? JSON.stringify(server.headers, null, 2) : '';
  editingExisting.value = true;
  editing.value = true;
};

const toggleServer = (name: string) => {
  const next = new Set(selectedServers.value);
  if (next.has(name)) {
    next.delete(name);
  } else {
    next.add(name);
  }
  emit('update:selectedServers', [...next]);
};

const parseArgs = () => argsText.value.split(/\s+/).map((arg) => arg.trim()).filter(Boolean);
const parseHeaders = () => {
  if (!headersText.value.trim()) return undefined;
  const parsed = JSON.parse(headersText.value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Headers must be a JSON object');
  }
  for (const [key, value] of Object.entries(parsed)) {
    if (typeof value !== 'string') {
      throw new Error(`headers.${key} must be a string`);
    }
  }
  return parsed as Record<string, string>;
};

const handleSave = async () => {
  if (!form.name.trim()) {
    showErrorToast(t('Name is required'));
    return;
  }
  saving.value = true;
  try {
    const headers = form.transport === 'stdio' ? undefined : parseHeaders();
    const payload: MCPServerInfo = {
      name: form.name.trim(),
      transport: form.transport as MCPTransport,
      enabled: form.enabled,
      description: form.description || undefined,
      command: form.transport === 'stdio' ? form.command : undefined,
      args: form.transport === 'stdio' ? parseArgs() : undefined,
      url: form.transport !== 'stdio' ? form.url : undefined,
      headers,
      env: form.env,
    };
    const saved = await saveMCPServer(payload);
    await loadServers();
    emit('update:selectedServers', [...new Set([...selectedServers.value, saved.name])]);
    editing.value = false;
    showSuccessToast(t('MCP server saved'));
  } catch (error) {
    console.error('Failed to save MCP server:', error);
    showErrorToast(t('Failed to save MCP server'));
  } finally {
    saving.value = false;
  }
};

const handleDelete = async (name: string) => {
  try {
    servers.value = await deleteMCPServer(name);
    emit('update:selectedServers', selectedServers.value.filter((serverName) => serverName !== name));
    showSuccessToast(t('MCP server deleted'));
  } catch (error) {
    console.error('Failed to delete MCP server:', error);
    showErrorToast(t('Failed to delete MCP server'));
  }
};

watch(open, (value) => {
  if (value) {
    loadServers();
  }
});
</script>
