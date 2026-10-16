import { createApp } from 'vue'
import App from './App.vue'
import './assets/global.css'
import './assets/theme.css'
import './utils/toast'
import i18n from './composables/useI18n'
import router from './router'
import { installReleaseCompatibility } from './utils/installReleaseCompatibility'

const app = createApp(App)

app.use(router)
app.use(i18n)
app.mount('#app')

const disposeReleaseCompatibility = installReleaseCompatibility(router)
if (import.meta.hot) import.meta.hot.dispose(disposeReleaseCompatibility)
