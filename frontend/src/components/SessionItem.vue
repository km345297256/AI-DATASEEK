<template>
  <div
    @click="handleSessionClick"
    class="group flex items-center rounded-[10px] cursor-pointer transition-colors w-full gap-[12px] h-[36px] flex-shrink-0 pointer-events-auto ps-[9px] pe-[2px] active:bg-[var(--fill-tsp-white-dark)]"
    :class="isCurrentSession ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'">

    <!-- 状态图标 -->
    <div class="shrink-0 size-[18px] flex items-center justify-center relative">
      <template v-if="session.status === SessionStatus.RUNNING || session.status === SessionStatus.PENDING">
        <div class="border rounded-full animate-spin" style="width: 18px; height: 18px; border-width: 2px; border-color: var(--fill-blue); border-top-color: var(--icon-brand);"></div>
      </template>
      <template v-else-if="session.status === SessionStatus.WAITING">
        <svg height="18" width="18" fill="none" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <g clip-path="url(#waiting-clip)">
            <circle cx="8" cy="8" r="6.5" stroke="var(--function-warning)" stroke-dasharray="2.44 1.62" stroke-width="1.5"></circle>
          </g>
          <defs><clipPath id="waiting-clip"><rect height="16" width="16" fill="white"></rect></clipPath></defs>
        </svg>
      </template>
      <template v-else>
        <MessageSquareText class="size-[18px] text-[var(--icon-tertiary)]" />
      </template>
    
    </div>

    <!-- 标题 -->
    <div class="flex-1 min-w-0 flex gap-[4px] items-center text-[14px] text-[var(--text-primary)]">
      <input
        v-if="isRenaming"
        ref="renameInputRef"
        v-model="renameDraft"
        class="h-7 w-full min-w-0 rounded-md border border-[var(--border-dark)] bg-[var(--background-menu-white)] px-2 text-sm text-[var(--text-primary)] outline-none"
        :aria-label="t('Task name')"
        maxlength="200"
        @click.stop
        @keydown.enter.prevent="saveRename"
        @keydown.escape.prevent="cancelRename"
        @blur="saveRename"
      />
      <span v-else class="truncate" :title="session.title || t('New Chat')">
        {{ session.title || t('New Chat') }}
      </span>
    </div>

    <!-- 省略号菜单 -->
    <div class="shrink-0 flex items-center gap-1">
      <div
        @click.stop="handleSessionMenuClick"
        class="group-hover:flex hidden size-8 rounded-[8px] cursor-pointer items-center justify-center hover:bg-[var(--fill-tsp-white-light)]"
        :class="isContextMenuOpen ? '!flex bg-[var(--fill-tsp-white-light)]' : ''"
        aria-expanded="false" aria-haspopup="dialog">
        <Ellipsis :size="18" class="text-[var(--icon-tertiary)]" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Ellipsis, MessageSquareText, Pencil, Trash } from 'lucide-vue-next';
import { computed, nextTick, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import { ListSessionItem, SessionStatus } from '../types/response';
import { useContextMenu, createDangerMenuItem, createMenuItem } from '../composables/useContextMenu';
import { useDialog } from '../composables/useDialog';
import { deleteSession, updateSessionTitle } from '../api/agent';
import { showSuccessToast, showErrorToast } from '../utils/toast';
import { eventBus } from '../utils/eventBus';
import { EVENT_SESSION_RENAMED } from '../constants/event';

interface Props {
  session: ListSessionItem;
}

const props = defineProps<Props>();

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { showContextMenu } = useContextMenu();
const { showConfirmDialog } = useDialog();
const isContextMenuOpen = ref(false);
const isRenaming = ref(false);
const renameDraft = ref('');
const renameInputRef = ref<HTMLInputElement>();
const renameSaving = ref(false);

const emit = defineEmits<{
  (e: 'deleted', sessionId: string): void;
  (e: 'renamed', payload: { sessionId: string; title: string }): void;
}>();

const currentSessionId = computed(() => {
  return route.params.sessionId as string;
});

const isCurrentSession = computed(() => {
  return currentSessionId.value === props.session.session_id;
});

const handleSessionClick = () => {
  if (isRenaming.value) return;
  router.push(`/chat/${props.session.session_id}`);
};

const startRename = async () => {
  renameDraft.value = props.session.title || t('New Chat');
  isRenaming.value = true;
  await nextTick();
  renameInputRef.value?.focus();
  renameInputRef.value?.select();
};

const cancelRename = () => {
  isRenaming.value = false;
  renameDraft.value = '';
};

const saveRename = async () => {
  if (!isRenaming.value || renameSaving.value) return;
  const title = renameDraft.value.trim();
  if (!title) {
    showErrorToast(t('Task name cannot be empty'));
    await nextTick();
    renameInputRef.value?.focus();
    return;
  }
  if (title === (props.session.title || t('New Chat'))) {
    cancelRename();
    return;
  }
  renameSaving.value = true;
  try {
    await updateSessionTitle(props.session.session_id, title);
    emit('renamed', { sessionId: props.session.session_id, title });
    eventBus.emit(EVENT_SESSION_RENAMED, { sessionId: props.session.session_id, title });
    showSuccessToast(t('Task renamed'));
    cancelRename();
  } catch (error) {
    console.error('Failed to rename session:', error);
    showErrorToast(t('Failed to rename task'));
    await nextTick();
    renameInputRef.value?.focus();
  } finally {
    renameSaving.value = false;
  }
};

const handleSessionMenuClick = (event: MouseEvent) => {
  event.stopPropagation();

  const target = event.currentTarget as HTMLElement;
  isContextMenuOpen.value = true;

  const menuItems = [];
  if (props.session.is_owner) {
    menuItems.push(createMenuItem('rename', t('Rename'), { icon: Pencil }));
  }
  menuItems.push(createDangerMenuItem('delete', t('Delete'), { icon: Trash }));

  showContextMenu(props.session.session_id, target, menuItems, (itemKey: string, _: string) => {
    if (itemKey === 'rename') {
      startRename();
    } else if (itemKey === 'delete') {
      showConfirmDialog({
        title: t('Are you sure you want to delete this session?'),
        content: t('The chat history of this session cannot be recovered after deletion.'),
        confirmText: t('Delete'),
        cancelText: t('Cancel'),
        confirmType: 'danger',
        onConfirm: () => {
          deleteSession(props.session.session_id).then(() => {
            showSuccessToast(t('Deleted successfully'));
            emit('deleted', props.session.session_id);
          }).catch(() => {
            showErrorToast(t('Failed to delete session'));
          });
          if (isCurrentSession.value) {
            router.push('/');
          }
        }
      })
    }
  }, (_: string) => {
    isContextMenuOpen.value = false;
  });
};
</script>
