<template>
    <div ref="composerRef" class="pb-3 relative bg-[var(--background-gray-main)]">
        <div
            v-if="slashMenuOpen"
            class="absolute left-2 right-2 z-50 flex max-h-[min(440px,52vh)] flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-[0_12px_32px_rgba(0,0,0,0.14)] sm:left-4 sm:right-auto sm:w-[390px]"
            :class="skillMenuPlacement === 'down' ? 'top-full mt-2' : 'bottom-full mb-2'"
            role="listbox"
            :aria-label="t('Available skills')">
            <div class="flex h-10 shrink-0 items-center gap-2 border-b border-[var(--border-main)] px-3">
                <Search :size="14" class="shrink-0 text-[var(--icon-tertiary)]" />
                <input
                    ref="skillSearchInputRef"
                    v-model="slashQuery"
                    type="search"
                    class="h-7 min-w-0 flex-1 bg-transparent text-[13px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]"
                    :placeholder="t('Search skills')"
                    @input="activeSkillIndex = 0"
                    @keydown="handleSkillSearchKeydown"
                />
                <span class="ml-auto shrink-0 rounded border border-[var(--border-main)] px-1.5 py-0.5 text-[10px] text-[var(--text-tertiary)]">ESC</span>
            </div>
            <div v-if="skillsLoading" class="min-h-0 flex-1 px-3 py-6 text-center text-sm text-[var(--text-tertiary)]">
                {{ t('Loading') }}...
            </div>
            <div v-else-if="filteredSkills.length === 0" class="min-h-0 flex-1 px-3 py-6 text-center text-sm text-[var(--text-tertiary)]">
                {{ t('No matching skills') }}
            </div>
            <div v-else class="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2 pt-1.5">
                <button
                    v-for="(skill, index) in filteredSkills"
                    :key="skill.name"
                    type="button"
                    role="option"
                    :data-skill-index="index"
                    :aria-selected="selectedSkills.includes(skill.name)"
                    class="flex min-h-11 w-full items-start gap-2 rounded-md px-2 py-1.5 text-left"
                    :class="index === activeSkillIndex ? 'bg-[var(--fill-tsp-white-main)]' : 'hover:bg-[var(--fill-tsp-white-light)]'"
                    @mouseenter="activeSkillIndex = index"
                    @mousedown.prevent="selectSlashSkill(skill)">
                    <Puzzle :size="15" class="mt-0.5 shrink-0 text-[var(--icon-secondary)]" />
                    <span class="min-w-0 flex-1">
                        <span class="flex items-center gap-2 text-[13px] font-medium leading-5 text-[var(--text-primary)]">
                            <span class="truncate">{{ skill.name }}</span>
                            <Check v-if="selectedSkills.includes(skill.name)" :size="14" class="shrink-0" />
                        </span>
                        <span v-if="skill.description" class="block truncate text-[11px] leading-4 text-[var(--text-tertiary)]">
                            {{ skill.description }}
                        </span>
                    </span>
                </button>
            </div>
            <div class="grid shrink-0 grid-cols-2 gap-1 border-t border-[var(--border-main)] p-2">
                <button
                    type="button"
                    class="flex h-8 items-center gap-2 rounded-md px-2 text-[13px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]"
                    @mousedown.prevent="openSkillPicker">
                    <CirclePlus :size="16" />
                    <span>{{ t('Add skill') }}</span>
                </button>
                <button
                    type="button"
                    class="flex h-8 items-center gap-2 rounded-md px-2 text-[13px] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-white-light)]"
                    @mousedown.prevent="openSkillManagement">
                    <Settings2 :size="16" />
                    <span>{{ t('Manage skills') }}</span>
                </button>
            </div>
        </div>
        <div
            class="flex flex-col gap-3 rounded-[22px] transition-all relative bg-[var(--fill-input-chat)] py-3 max-h-[70vh] shadow-[0px_12px_32px_0px_rgba(0,0,0,0.02)] border border-black/8 dark:border-[var(--border-main)]">
            <ChatBoxFiles ref="chatBoxFileListRef" :attachments="attachments" />
            <div v-if="visibleSelectedSkills.length" class="flex flex-wrap gap-1.5 px-4">
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
            <div class="overflow-y-auto pl-4 pr-2">
                <textarea
                    ref="textareaRef"
                    class="flex rounded-md border-input focus-visible:outline-none focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 overflow-y-auto flex-1 bg-transparent p-0 pt-[1px] border-0 focus-visible:ring-0 focus-visible:ring-offset-0 w-full placeholder:text-[var(--text-disable)] text-[15px] shadow-none resize-y min-h-[46px] max-h-[55vh]"
                    :rows="rows" :value="modelValue"
                    @input="handleInput"
                    @compositionstart="isComposing = true" @compositionend="isComposing = false"
                    @keydown="handleKeydown" :placeholder="t('Give AI-DataSeek a task to work on...')"></textarea>
            </div>
            <footer class="flex flex-row justify-between w-full px-2 sm:px-3">
                <div class="flex gap-1 sm:gap-2 pr-1 sm:pr-2 items-center">
                    <div ref="actionMenuRef" class="relative">
                        <button
                            type="button"
                            class="relative inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[var(--border-main)] text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] sm:h-8 sm:w-8"
                            :class="actionMenuOpen ? 'bg-[var(--fill-tsp-gray-main)]' : ''"
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
                                <button type="button" class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]" role="menuitem" @click="handleUploadFileAction">
                                    <Paperclip :size="15" class="shrink-0" />
                                    <span class="truncate">{{ t('Upload file') }}</span>
                                </button>
                                <button type="button" class="flex h-9 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]" role="menuitem" @click="handleUploadLargeFileAction">
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
                                class="absolute left-[160px] z-[61] flex w-[calc(100vw-180px)] max-w-[330px] flex-col overflow-hidden rounded-lg border border-[var(--border-main)] bg-[var(--background-menu-white)] shadow-[0_12px_32px_rgba(0,0,0,0.16)] sm:left-[196px] sm:w-[330px]"
                                :class="skillMenuPlacement === 'down' ? 'top-full mt-2 max-h-[min(390px,52vh)]' : 'bottom-full mb-2 max-h-[min(390px,52vh)]'"
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
                    <button @click="mcpDialogOpen = true"
                        class="relative rounded-full border border-[var(--border-main)] inline-flex items-center justify-center gap-1 clickable cursor-pointer text-xs text-[var(--text-secondary)] hover:bg-[var(--fill-tsp-gray-main)] w-10 h-10 sm:w-8 sm:h-8 p-0 data-[popover-trigger]:bg-[var(--fill-tsp-gray-main)] shrink-0"
                        aria-expanded="false" aria-haspopup="dialog"
                        :title="t('MCP Tools')">
                        <PlugZap :size="16" />
                        <span v-if="selectedMcpServers.length > 0"
                            class="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--Button-primary-black)] px-1 text-[10px] leading-none text-[var(--text-onblack)]">
                            {{ selectedMcpServers.length }}
                        </span>
                    </button>
                </div>
                <div class="flex gap-2">
                    <button v-if="!isRunning || sendEnabled || hideStopButton"
                        class="whitespace-nowrap text-sm font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 text-primary-foreground hover:bg-primary/90 p-0 w-10 h-10 sm:w-8 sm:h-8 rounded-full flex items-center justify-center transition-colors hover:opacity-90"
                        :class="!sendEnabled ? 'cursor-not-allowed bg-[var(--fill-tsp-white-dark)]' : 'cursor-pointer bg-[var(--Button-primary-black)]'"
                        @click="handleSubmit">
                        <SendIcon :disabled="!sendEnabled" />
                    </button>
                    <button v-else-if="!hideStopButton" @click="handleStop"
                        class="inline-flex items-center justify-center whitespace-nowrap text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring bg-[var(--Button-primary-black)] text-[var(--text-onblack)] gap-[4px] hover:opacity-90 rounded-full p-0 w-10 h-10 sm:w-8 sm:h-8">
                        <div class="w-[10px] h-[10px] bg-[var(--icon-onblack)] rounded-[2px]">
                        </div>
                    </button>
                </div>
            </footer>
        </div>
        <SkillDialog v-model:open="skillDialogOpen" v-model:selected-skills="selectedSkills" />
        <MCPDialog v-model:open="mcpDialogOpen" v-model:selected-servers="selectedMcpServers" />
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
import { EVENT_SKILL_PREFERENCES_UPDATED } from '../constants/event';

const { t } = useI18n();
const { openSettingsDialog } = useSettingsDialog();
const hasTextInput = ref(false);
const isComposing = ref(false);
const chatBoxFileListRef = ref();
const skillDialogOpen = ref(false);
const mcpDialogOpen = ref(false);
const composerRef = ref<HTMLElement>();
const textareaRef = ref<HTMLTextAreaElement>();
const skillSearchInputRef = ref<HTMLInputElement>();
const actionMenuRef = ref<HTMLElement>();
const actionSkillSearchInputRef = ref<HTMLInputElement>();
const availableSkills = ref<SkillInfo[]>([]);
const skillsLoading = ref(false);
const skillsLoaded = ref(false);
const slashMenuOpen = ref(false);
const slashQuery = ref('');
const slashRange = ref<{ start: number; end: number } | null>(null);
const activeSkillIndex = ref(0);
const autoEnabledSkillNames = ref(new Set<string>());
const actionMenuOpen = ref(false);
const actionSkillMenuOpen = ref(false);
const actionSkillQuery = ref('');

const props = defineProps<{
    modelValue: string;
    rows: number;
    isRunning: boolean;
    attachments: FileInfo[];
    selectedSkills?: string[];
    selectedMcpServers?: string[];
    skillMenuPlacement?: 'up' | 'down';
    hideStopButton?: boolean;
    allowSendFilesOnly?: boolean;
}>();

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
    const hasFiles = (props.attachments?.length ?? 0) > 0;
    const allUploaded = chatBoxFileListRef.value?.isAllUploaded ?? true;
    if (props.allowSendFilesOnly) {
        return hasTextInput.value || (hasFiles && allUploaded);
    }
    return hasTextInput.value && (!hasFiles || allUploaded);
});

const filteredSkills = computed(() => {
    const query = slashQuery.value.trim().toLocaleLowerCase();
    const manuallySelectable = availableSkills.value.filter(
        (skill) => !autoEnabledSkillNames.value.has(skill.name.trim().toLowerCase()),
    );
    if (!query) return manuallySelectable;
    return manuallySelectable.filter((skill) =>
        [skill.name, skill.description, ...skill.triggers]
            .some((value) => value?.toLocaleLowerCase().includes(query)),
    );
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
        slashMenuOpen.value = false;
        actionSkillMenuOpen.value = false;
    } finally {
        skillsLoading.value = false;
    }
};

const updateSlashCommand = (value: string, cursor: number | null) => {
    if (cursor === null) {
        slashMenuOpen.value = false;
        return;
    }
    const beforeCursor = value.slice(0, cursor);
    const match = beforeCursor.match(/(?:^|\s)\/([^\s/]*)$/);
    if (!match) {
        slashMenuOpen.value = false;
        slashRange.value = null;
        return;
    }
    const query = match[1] ?? '';
    slashQuery.value = query;
    slashRange.value = { start: cursor - query.length - 1, end: cursor };
    activeSkillIndex.value = 0;
    slashMenuOpen.value = true;
    loadAvailableSkills();
    nextTick(() => {
        skillSearchInputRef.value?.focus();
        skillSearchInputRef.value?.select();
    });
};

const handleInput = (event: Event) => {
    const target = event.target as HTMLTextAreaElement;
    emit('update:modelValue', target.value);
    updateSlashCommand(target.value, target.selectionStart);
};

const handleKeydown = (event: KeyboardEvent) => {
    if (event.isComposing || isComposing.value || event.keyCode === 229) {
        // Let the input method commit the selected candidate without triggering an action.
        return;
    }

    if (slashMenuOpen.value) {
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            if (filteredSkills.value.length) {
                activeSkillIndex.value = (activeSkillIndex.value + 1) % filteredSkills.value.length;
                scrollActiveSkillIntoView();
            }
            return;
        }
        if (event.key === 'ArrowUp') {
            event.preventDefault();
            if (filteredSkills.value.length) {
                activeSkillIndex.value = (activeSkillIndex.value - 1 + filteredSkills.value.length) % filteredSkills.value.length;
                scrollActiveSkillIntoView();
            }
            return;
        }
        if ((event.key === 'Enter' || event.key === 'Tab') && filteredSkills.value.length) {
            event.preventDefault();
            selectSlashSkill(filteredSkills.value[activeSkillIndex.value]);
            return;
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            slashMenuOpen.value = false;
            return;
        }
    }

    if (event.key !== 'Enter') return;
    if (!event.ctrlKey && !event.metaKey) {
        return;
    }

    // Ctrl/Command + Enter submits; plain Enter keeps textarea newline behavior.
    if (sendEnabled.value) {
        event.preventDefault();
        handleSubmit();
    }
};

const handleSkillSearchKeydown = (event: KeyboardEvent) => {
    event.stopPropagation();
    if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (filteredSkills.value.length) {
            activeSkillIndex.value = (activeSkillIndex.value + 1) % filteredSkills.value.length;
            scrollActiveSkillIntoView();
        }
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (filteredSkills.value.length) {
            activeSkillIndex.value = (activeSkillIndex.value - 1 + filteredSkills.value.length) % filteredSkills.value.length;
            scrollActiveSkillIntoView();
        }
    } else if ((event.key === 'Enter' || event.key === 'Tab') && filteredSkills.value.length) {
        event.preventDefault();
        selectSlashSkill(filteredSkills.value[activeSkillIndex.value]);
    } else if (event.key === 'Escape') {
        event.preventDefault();
        slashMenuOpen.value = false;
        nextTick(() => textareaRef.value?.focus());
    }
};

const scrollActiveSkillIntoView = async () => {
    await nextTick();
    composerRef.value
        ?.querySelector<HTMLElement>(`[data-skill-index="${activeSkillIndex.value}"]`)
        ?.scrollIntoView({ block: 'nearest' });
};

const selectSlashSkill = async (skill: SkillInfo) => {
    const range = slashRange.value;
    if (!range) return;
    const nextValue = props.modelValue.slice(0, range.start) + props.modelValue.slice(range.end);
    if (!selectedSkills.value.includes(skill.name)) {
        selectedSkills.value = [...selectedSkills.value, skill.name];
    }
    emit('update:modelValue', nextValue);
    slashMenuOpen.value = false;
    slashRange.value = null;
    await nextTick();
    textareaRef.value?.focus();
    textareaRef.value?.setSelectionRange(range.start, range.start);
};

const removeSkill = (name: string) => {
    selectedSkills.value = selectedSkills.value.filter((skill) => skill !== name);
};

const toggleActionMenu = () => {
    slashMenuOpen.value = false;
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
    slashMenuOpen.value = false;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    skillDialogOpen.value = true;
};

const openSkillManagement = () => {
    slashMenuOpen.value = false;
    actionMenuOpen.value = false;
    closeActionSkillMenu();
    openSettingsDialog('skills');
};

const handleSubmit = () => {
    if (!sendEnabled.value) return;
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

watch(filteredSkills, () => {
    if (activeSkillIndex.value >= filteredSkills.value.length) activeSkillIndex.value = 0;
});

watch(skillDialogOpen, (isOpen, wasOpen) => {
    if (!isOpen && wasOpen) skillsLoaded.value = false;
});

const handleDocumentPointerDown = (event: PointerEvent) => {
    const target = event.target as Node;
    if (slashMenuOpen.value && !composerRef.value?.contains(target)) {
        slashMenuOpen.value = false;
    }
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
    eventBus.on(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
    loadAutoEnabledSkills();
});
onUnmounted(() => {
    document.removeEventListener('pointerdown', handleDocumentPointerDown);
    document.removeEventListener('keydown', handleDocumentKeydown);
    eventBus.off(EVENT_SKILL_PREFERENCES_UPDATED, handleSkillPreferencesUpdated);
});
</script>
