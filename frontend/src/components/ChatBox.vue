<template>
    <div ref="composerRef" class="pb-3 relative bg-[var(--background-gray-main)]">
        <div
            class="relative flex flex-col bg-[var(--fill-input-chat)] shadow-[0px_12px_32px_0px_rgba(0,0,0,0.02)] transition-all border border-black/8 dark:border-[var(--border-main)]"
            :class="compactComposer
                ? 'min-h-[56px] max-h-[70vh] gap-1 rounded-[18px] py-2'
                : 'max-h-[70vh] gap-3 rounded-[22px] py-3'">
            <ChatBoxFiles v-if="showFileActions || attachments.length" ref="chatBoxFileListRef" :attachments="attachments" />
            <div v-if="visibleSelectedSkills.length" class="flex max-h-20 flex-wrap gap-1.5 overflow-y-auto px-4">
                <div
                    v-for="skillName in visibleSelectedSkills"
                    :key="skillName"
                    class="inline-flex h-8 max-w-full items-center gap-1.5 rounded-md border border-[var(--border-main)] bg-[var(--background-menu-white)] px-2 text-xs text-[var(--text-primary)] shadow-sm">
                    <Puzzle :size="14" class="shrink-0 text-[var(--icon-secondary)]" />
                    <span class="truncate font-medium">{{ skillName }}</span>
                    <button
                        type="button"
                        class="ml-0.5 flex size-5 shrink-0 items-center justify-center rounded hover:bg-[var(--fill-tsp-white-dark)]"
                        :aria-label="t('Remove skill', { name: skillName })"
                        :title="t('Remove selected skill')"
                        @click="removeSkill(skillName)">
                        <X :size="13" />
                    </button>
                </div>
            </div>
            <div
                class="overflow-y-auto"
                :class="compactComposer ? 'min-h-10 pl-[52px] pr-[52px]' : 'pl-4 pr-2'">
                <textarea
                    ref="textareaRef"
                    class="flex w-full flex-1 rounded-md border-0 border-input bg-transparent p-0 pt-[1px] text-[15px] shadow-none placeholder:text-[var(--text-disable)] focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50"
                    :class="compactComposer
                        ? '!h-10 !min-h-10 !max-h-10 !resize-none !overflow-y-auto !py-[9px] leading-5'
                        : 'min-h-[46px] max-h-[55vh] resize-y overflow-y-auto'"
                    :rows="rows" :value="modelValue"
                    :disabled="disabled"
                    @input="handleInput"
                    @keydown="handleKeydown" :placeholder="placeholder || t('Give AI-DataSeek a task to work on...')"></textarea>
            </div>
            <footer
                class="flex w-full flex-row justify-between px-2 sm:px-3"
                :class="compactComposer ? 'pointer-events-none absolute inset-x-0 bottom-2 sm:bottom-3' : ''">
                <div
                    class="flex items-center gap-1 pr-1 sm:gap-2 sm:pr-2"
                    :class="compactComposer ? 'pointer-events-auto' : ''">
                    <div ref="actionMenuRef" class="relative">
                        <button
                            type="button"
                            class="relative inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[var(--border-main)] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] sm:h-8 sm:w-8"
                            :class="actionMenuOpen ? 'bg-[var(--fill-tsp-gray-main)]' : ''"
                            :disabled="disabled"
                            :aria-expanded="actionMenuOpen"
                            aria-haspopup="menu"
                            :title="t('Add content')"
                            @click="toggleActionMenu"
                        >
                            <Plus :size="18" />
                            <span v-if="visibleSelectedSkills.length > 0"
                                class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--Button-primary-black)] px-1 text-[10px] leading-none text-[var(--text-onblack)]">
                                {{ visibleSelectedSkills.length }}
                            </span>
                        </button>

                        <template v-if="actionMenuOpen">
                            <div
                                class="absolute left-0 z-[60] w-[154px] overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] p-1.5 shadow-[0_12px_32px_rgba(0,0,0,0.16)] sm:w-[190px]"
                                :class="skillMenuPlacement === 'down' ? 'top-full mt-2' : 'bottom-full mb-2'"
                                role="menu"
                            >
                                <button v-if="showFileActions" type="button" class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]" role="menuitem" @click="handleUploadFileAction">
                                    <Paperclip :size="15" class="shrink-0" />
                                    <span class="truncate">{{ t('Upload file') }}</span>
                                </button>
                                <button v-if="showFileActions" type="button" class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]" role="menuitem" @click="handleUploadLargeFileAction">
                                    <UploadCloud :size="15" class="shrink-0" />
                                    <span class="truncate">{{ t('Large file upload') }}</span>
                                </button>
                                <button
                                    type="button"
                                    class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
                                    :class="actionSkillMenuOpen ? 'bg-[var(--fill-tsp-white-main)]' : ''"
                                    role="menuitem"
                                    aria-haspopup="menu"
                                    :aria-expanded="actionSkillMenuOpen"
                                    @mouseenter="openActionSkillMenu"
                                    @click="focusActionSkillMenu"
                                >
                                    <Puzzle :size="15" class="shrink-0" />
                                    <span class="min-w-0 flex-1 truncate">{{ t('Use skill') }}</span>
                                    <ChevronRight :size="14" class="shrink-0 text-[var(--icon-tertiary)]" />
                                </button>
                            </div>

                            <div
                                v-if="actionSkillMenuOpen"
                                class="fixed left-3 right-3 z-[61] flex max-w-[330px] flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-[0_12px_32px_rgba(0,0,0,0.16)] lg:absolute lg:left-[196px] lg:right-auto lg:w-[330px]"
                                :class="skillMenuPlacement === 'down' ? 'top-20 max-h-[min(390px,calc(100dvh-100px))] lg:top-full lg:mt-2 lg:max-h-[min(390px,52vh)]' : 'bottom-24 max-h-[min(390px,calc(100dvh-112px))] lg:bottom-full lg:mb-2 lg:max-h-[min(390px,52vh)]'"
                                role="menu"
                                :aria-label="t('Available skills')"
                            >
                                <div class="flex h-10 shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-2.5">
                                    <Search :size="14" class="shrink-0 text-[var(--icon-tertiary)]" />
                                    <input
                                        ref="actionSkillSearchInputRef"
                                        v-model="actionSkillQuery"
                                        type="search"
                                        class="h-7 min-w-0 flex-1 bg-transparent text-[13px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]"
                                        :placeholder="t('Search skills')"
                                        @keydown.esc.prevent="closeActionSkillMenu"
                                    />
                                </div>
                                <div v-if="skillsLoading" class="px-3 py-5 text-center text-xs text-[var(--text-tertiary)]">{{ t('Loading') }}...</div>
                                <div v-else-if="actionFilteredSkills.length === 0" class="px-3 py-5 text-center text-xs text-[var(--text-tertiary)]">{{ t('No matching skills') }}</div>
                                <div v-else class="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2.5 pt-1.5">
                                    <button
                                        v-for="skill in actionFilteredSkills"
                                        :key="skill.name"
                                        type="button"
                                        class="flex min-h-10 w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-[var(--fill-tsp-white-light)]"
                                        role="menuitemcheckbox"
                                        :aria-checked="selectedSkills.includes(skill.name)"
                                        @click="toggleActionSkill(skill)"
                                    >
                                        <Puzzle :size="14" class="mt-0.5 shrink-0 text-[var(--icon-secondary)]" />
                                        <span class="min-w-0 flex-1">
                                            <span class="flex items-center gap-1.5 text-[13px] font-medium leading-4 text-[var(--text-primary)]">
                                                <span class="truncate">{{ skill.name }}</span>
                                                <Check v-if="selectedSkills.includes(skill.name)" :size="13" class="shrink-0" />
                                            </span>
                                            <span v-if="skill.description" class="mt-0.5 block truncate text-[11px] leading-4 text-[var(--text-tertiary)]">{{ skill.description }}</span>
                                        </span>
                                    </button>
                                </div>
                                <div class="grid shrink-0 grid-cols-2 gap-1 border-t border-[var(--border-main)] p-2">
                                    <button type="button" class="flex h-8 items-center gap-1.5 rounded-md px-2 text-[12px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="openSkillPicker">
                                        <CirclePlus :size="14" />
                                        <span class="truncate">{{ t('Add skill') }}</span>
                                    </button>
                                    <button type="button" class="flex h-8 items-center gap-1.5 rounded-md px-2 text-[12px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]" @click="openSkillManagement">
                                        <Settings2 :size="14" />
                                        <span class="truncate">{{ t('Manage skills') }}</span>
                                    </button>
                                </div>
                            </div>
                        </template>
                    </div>
                    <button v-if="showMcpActions" @click="mcpDialogOpen = true"
                        class="relative rounded-full border border-[var(--border-main)] inline-flex items-center justify-center gap-1 clickable cursor-pointer text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] w-10 h-10 sm:w-8 sm:h-8 p-0 data-[popover-trigger]:bg-[var(--fill-tsp-gray-main)] shrink-0"
                        :disabled="disabled"
                        aria-expanded="false" aria-haspopup="dialog"
                        :title="t('MCP Tools')">
                        <PlugZap :size="16" />
                        <span v-if="selectedMcpServers.length > 0"
                            class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--Button-primary-black)] px-1 text-[10px] leading-none text-[var(--text-onblack)]">
                            {{ selectedMcpServers.length }}
                        </span>
                    </button>
                </div>
                <div class="flex gap-2" :class="compactComposer ? 'pointer-events-auto' : ''">
                    <button v-if="!isRunning || sendEnabled || hideStopButton"
                        type="button"
                        class="whitespace-nowrap text-sm font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 text-primary-foreground hover:bg-primary/90 p-0 w-10 h-10 sm:w-8 sm:h-8 rounded-full flex items-center justify-center transition-colors hover:opacity-90"
                        :class="!sendEnabled ? 'cursor-not-allowed bg-[var(--fill-tsp-white-dark)]' : 'cursor-pointer bg-[var(--Button-primary-black)]'"
                        :disabled="!sendEnabled"
                        :aria-label="t('Send message')"
                        :title="t('Send message')"
                        @click="handleSubmit">
                        <SendIcon :disabled="!sendEnabled" />
                    </button>
                    <button v-else-if="!hideStopButton" type="button" :aria-label="t('Stop task')" :title="t('Stop task')" @click="handleStop"
                        class="inline-flex items-center justify-center whitespace-nowrap text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring bg-[var(--Button-primary-black)] text-[var(--text-onblack)] gap-[4px] hover:opacity-90 rounded-full p-0 w-10 h-10 sm:w-8 sm:h-8">
                        <span class="sr-only">{{ t('Stop task') }}</span>
                        <div class="w-[10px] h-[10px] bg-[var(--icon-onblack)] rounded-[2px]">
                        </div>
                    </button>
                </div>
            </footer>
        </div>
        <SkillDialog v-model:open="skillDialogOpen" v-model:selected-skills="selectedSkills" />
        <MCPDialog v-if="showMcpActions" v-model:open="mcpDialogOpen" v-model:selected-servers="selectedMcpServers" />
    </div>
</template>

<script setup lang="ts">
import { ref, watch, computed, nextTick, onMounted, onUnmounted } from 'vue';
import SendIcon from './icons/SendIcon.vue';
import { useI18n } from 'vue-i18n';
import ChatBoxFiles from './ChatBoxFiles.vue';
import SkillDialog from './SkillDialog.vue';
import MCPDialog from './MCPDialog.vue';
import { Check, ChevronRight, CirclePlus, Paperclip, PlugZap, Plus, Puzzle, Search, Settings2, UploadCloud, X } from 'lucide-vue-next';
import type { FileInfo } from '../api/file';
import { getSkillPreferences, listSkills, type SkillInfo } from '../api/skill';
import { showErrorToast } from '../utils/toast';
import { useSettingsDialog } from '../composables/useSettingsDialog';
import { eventBus } from '../utils/eventBus';
import { EVENT_SKILL_CATALOG_UPDATED, EVENT_SKILL_PREFERENCES_UPDATED } from '../constants/event';

const { t } = useI18n();
const { openSettingsDialog } = useSettingsDialog();
const hasTextInput = ref(false);
const chatBoxFileListRef = ref();
const skillDialogOpen = ref(false);
const mcpDialogOpen = ref(false);
const composerRef = ref<HTMLElement>();
const textareaRef = ref<HTMLTextAreaElement>();
const actionMenuRef = ref<HTMLElement>();
const actionSkillSearchInputRef = ref<HTMLInputElement>();
const availableSkills = ref<SkillInfo[]>([]);
const skillsLoading = ref(false);
const skillsLoaded = ref(false);
const autoEnabledSkillNames = ref(new Set<string>());
const actionMenuOpen = ref(false);
const actionSkillMenuOpen = ref(false);
const actionSkillQuery = ref('');

const props = withDefaults(defineProps<{
    modelValue: string;
    rows: number;
    isRunning: boolean;
    attachments: FileInfo[];
    selectedSkills?: string[];
    selectedMcpServers?: string[];
    skillMenuPlacement?: 'up' | 'down';
    hideStopButton?: boolean;
    allowSendFilesOnly?: boolean;
    placeholder?: string;
    showFileActions?: boolean;
    showMcpActions?: boolean;
    disabled?: boolean;
    submitOnEnter?: boolean;
    compactComposer?: boolean;
}>(), {
    showFileActions: true,
    showMcpActions: true,
    disabled: false,
    submitOnEnter: false,
    compactComposer: false,
});

const emit = defineEmits<{
    (e: 'update:modelValue', value: string): void;
    (e: 'update:selectedSkills', value: string[]): void;
    (e: 'update:selectedMcpServers', value: string[]): void;
    (e: 'submit'): void;
    (e: 'stop'): void;
}>();

const selectedSkills = computed({
    get: () => props.selectedSkills ?? [],
    set: (value: string[]) => emit('update:selectedSkills', value),
});

const selectedMcpServers = computed({
    get: () => props.selectedMcpServers ?? [],
    set: (value: string[]) => emit('update:selectedMcpServers', value),
});

const visibleSelectedSkills = computed(() => selectedSkills.value.filter(
    (name) => !autoEnabledSkillNames.value.has(name.trim().toLowerCase()),
));

const sendEnabled = computed(() => {
    if (props.disabled) return false;
    const hasFiles = (props.attachments?.length ?? 0) > 0;
    const allUploaded = chatBoxFileListRef.value?.isAllUploaded ?? true;
    if (props.allowSendFilesOnly) {
        return hasTextInput.value || (hasFiles && allUploaded);
    }
    return hasTextInput.value && (!hasFiles || allUploaded);
});

const actionFilteredSkills = computed(() => {
    const query = actionSkillQuery.value.trim().toLocaleLowerCase();
    const manuallySelectable = availableSkills.value.filter(
        (skill) => !autoEnabledSkillNames.value.has(skill.name.trim().toLowerCase()),
    );
    if (!query) return manuallySelectable;
    return manuallySelectable.filter((skill) =>
        [skill.name, skill.description, ...skill.triggers]
            .some((value) => value?.toLocaleLowerCase().includes(query)),
    );
});

const setAutoEnabledSkills = (names: string[]) => {
    autoEnabledSkillNames.value = new Set(names.map((name) => name.trim().toLowerCase()));
};

const handleSkillPreferencesUpdated = (payload: unknown) => {
    if (Array.isArray(payload) && payload.every((name) => typeof name === 'string')) {
        setAutoEnabledSkills(payload);
    }
};

const handleSkillCatalogUpdated = () => {
    skillsLoaded.value = false;
    if (actionSkillMenuOpen.value) void loadAvailableSkills();
};

const loadAutoEnabledSkills = async () => {
    try {
        setAutoEnabledSkills(await getSkillPreferences());
    } catch (error) {
        console.error('Failed to load automatic skill preferences:', error);
    }
};

const loadAvailableSkills = async () => {
    if (skillsLoaded.value || skillsLoading.value) return;
    skillsLoading.value = true;
    try {
        const [skills, preferences] = await Promise.all([listSkills(), getSkillPreferences()]);
        availableSkills.value = skills;
        setAutoEnabledSkills(preferences);
        skillsLoaded.value = true;
    } catch (error) {
        console.error('Failed to load skills:', error);
        showErrorToast(t('Failed to load skills'));
        actionSkillMenuOpen.value = false;
    } finally {
        skillsLoading.value = false;
    }
};

const handleInput = (event: Event) => {
    const target = event.target as HTMLTextAreaElement;
    emit('update:modelValue', target.value);
};

const handleKeydown = (event: KeyboardEvent) => {
    if (event.isComposing || event.keyCode === 229) {
        // Let the input method commit the selected candidate without triggering an action.
        return;
    }

    if (event.key !== 'Enter') return;
    if (props.submitOnEnter) {
        if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
        event.preventDefault();
        if (sendEnabled.value) handleSubmit();
        return;
    }
    if (!event.ctrlKey && !event.metaKey) {
        return;
    }

    // Ctrl/Command + Enter submits; plain Enter keeps textarea newline behavior.
    if (sendEnabled.value) {
        event.preventDefault();
        handleSubmit();
    }
};

const removeSkill = (name: string) => {
    selectedSkills.value = selectedSkills.value.filter((skill) => skill !== name);
};

const toggleActionMenu = () => {
    if (props.disabled) return;
    actionMenuOpen.value = !actionMenuOpen.value;
    if (!actionMenuOpen.value) closeActionSkillMenu();
};

const openActionSkillMenu = () => {
    if (actionSkillMenuOpen.value) return;
    actionSkillMenuOpen.value = true;
    actionSkillQuery.value = '';
    loadAvailableSkills();
};

const focusActionSkillMenu = () => {
    openActionSkillMenu();
    nextTick(() => actionSkillSearchInputRef.value?.focus());
};

const closeActionSkillMenu = () => {
    actionSkillMenuOpen.value = false;
    actionSkillQuery.value = '';
};

const toggleActionSkill = (skill: SkillInfo) => {
    if (selectedSkills.value.includes(skill.name)) {
        removeSkill(skill.name);
    } else {
        selectedSkills.value = [...selectedSkills.value, skill.name];
    }
};

const openSkillPicker = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    skillDialogOpen.value = true;
};

const openSkillManagement = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    openSettingsDialog('skills');
};

const handleSubmit = () => {
    if (!sendEnabled.value) return;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    emit('submit');
};

const handleStop = () => {
    emit('stop');
};

const uploadFile = () => {
    chatBoxFileListRef.value?.uploadFile();
};

const uploadLargeFile = () => {
    chatBoxFileListRef.value?.uploadLargeFile();
};

const handleUploadFileAction = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    uploadFile();
};

const handleUploadLargeFileAction = () => {
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    uploadLargeFile();
};

watch(() => props.modelValue, (value) => {
    hasTextInput.value = value.trim() !== '';
});

watch(skillDialogOpen, (isOpen, wasOpen) => {
    if (!isOpen && wasOpen) skillsLoaded.value = false;
});

watch(() => props.disabled, (disabled) => {
    if (!disabled) return;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
});

const handleDocumentPointerDown = (event: PointerEvent) => {
    const target = event.target as Node;
    if (actionMenuOpen.value && !actionMenuRef.value?.contains(target)) {
        actionMenuOpen.value = false;
        closeActionSkillMenu();
    }
};

const handleDocumentKeydown = (event: KeyboardEvent) => {
    if (event.key !== 'Escape' || !actionMenuOpen.value) return;
    if (actionSkillMenuOpen.value) closeActionSkillMenu();
    else actionMenuOpen.value = false;
};

onMounted(() => {
    document.addEventListener('pointerdown', handleDocumentPointerDown);
    document.addEventListener('keydown', handleDocumentKeydown);
    eventBus.on(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated);
    eventBus.on(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
    loadAutoEnabledSkills();
});
onUnmounted(() => {
    document.removeEventListener('pointerdown', handleDocumentPointerDown);
    document.removeEventListener('keydown', handleDocumentKeydown);
    eventBus.off(EVENT_SKILL_CATALOG_UPDATED, handleSkillCatalogUpdated);
    eventBus.off(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
});
</script>
