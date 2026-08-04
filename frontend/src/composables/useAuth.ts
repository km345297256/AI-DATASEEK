import { computed, ref } from 'vue'
import type {
  User,
  LoginRequest,
  RegisterRequest,
  LoginResponse,
  RegisterResponse
} from '../api/auth'

const SYSTEM_USER: User = {
  id: 'anonymous',
  fullname: 'AI-DataSeek System',
  email: 'system@localhost',
  role: 'admin',
  is_active: true,
  registration_status: 'approved',
  created_at: '1970-01-01T00:00:00.000Z',
  updated_at: '1970-01-01T00:00:00.000Z',
}

// Remove credentials left by older authenticated deployments. The API client
// also strips these headers, so they can never be sent to platform endpoints.
if (typeof window !== 'undefined') {
  window.localStorage.removeItem('access_token')
  window.localStorage.removeItem('refresh_token')
}

// Keep the former composable contract for pages that display actor/role data.
// Authentication itself is permanently satisfied by the single system actor.
const currentUser = ref<User>(SYSTEM_USER)
const isAuthenticated = ref(true)
const isLoading = ref(false)
const authError = ref<string | null>(null)

export function useAuth() {
  const initAuth = async () => {
    currentUser.value = SYSTEM_USER
    isAuthenticated.value = true
    authError.value = null
  }

  const loadCurrentUser = async () => {
    await initAuth()
  }

  // Legacy actions are local no-ops. Login UI is no longer routable, but these
  // signatures keep dormant components type-safe until they are removed.
  const login = async (_credentials: LoginRequest): Promise<LoginResponse> => {
    await initAuth()
    return {
      user: SYSTEM_USER,
      access_token: '',
      refresh_token: '',
      token_type: 'none',
    }
  }

  const register = async (_data: RegisterRequest): Promise<RegisterResponse> => {
    return {
      user: SYSTEM_USER,
      verification_required: false,
      message: 'Authentication is disabled',
    }
  }

  const logout = async (_silent: boolean = false) => {
    await initAuth()
  }

  const clearAuth = () => {
    currentUser.value = SYSTEM_USER
    isAuthenticated.value = true
    isLoading.value = false
    authError.value = null
  }

  const refreshAuthToken = async (): Promise<boolean> => {
    return true
  }

  /**
   * Check if user has specific role
   */
  const hasRole = (role: string): boolean => {
    return currentUser.value?.role === role
  }

  /**
   * Check if user is admin
   */
  const isAdmin = computed(() => hasRole('admin'))

  /**
   * Check if user account is active
   */
  const isActive = computed(() => currentUser.value?.is_active === true)

  /**
   * Clear authentication error
   */
  const clearError = () => {
    authError.value = null
  }

  return {
    // State
    currentUser: computed(() => currentUser.value),
    isAuthenticated: computed(() => isAuthenticated.value),
    isLoading: computed(() => isLoading.value),
    authError: computed(() => authError.value),
    isAdmin,
    isActive,
    
    // Actions
    login,
    register,
    logout,
    initAuth,
    loadCurrentUser,
    refreshAuthToken,
    hasRole,
    clearError,
    clearAuth
  }
} 
