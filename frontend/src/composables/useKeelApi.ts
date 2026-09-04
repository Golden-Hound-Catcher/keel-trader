/**
 * Keel read-only API client (Phase U1).
 *
 * Uses Keel `/api/v1/*` and `/health` only.
 * Optional Bearer via VITE_KEEL_API_TOKEN when backend KEEL_API_TOKEN is set.
 * Forbidden: data/*.json, legacy r20_backend private/admin routes.
 */
import { ref } from 'vue'

export function useKeelApi() {
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function keelFetch<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
    loading.value = true
    error.value = null
    try {
      const headers: Record<string, string> = {
        Accept: 'application/json',
        ...(options.headers as Record<string, string> | undefined),
      }
      // Explicitly omit Content-Type on GET; never attach X-R20-Session
      if (options.method && options.method !== 'GET' && options.body) {
        headers['Content-Type'] = headers['Content-Type'] || 'application/json'
      }

      const apiToken = (import.meta.env.VITE_KEEL_API_TOKEN as string | undefined)?.trim()
      if (apiToken) {
        headers['Authorization'] = `Bearer ${apiToken}`
      }

      const resp = await fetch(path, {
        ...options,
        headers,
      })

      let data: any = {}
      try {
        data = await resp.json()
      } catch {
        // empty / non-JSON body
      }

      if (!resp.ok) {
        const detail = Array.isArray(data.detail)
          ? data.detail.map((x: any) => `${(x.loc || []).slice(1).join('.') || 'request'}: ${x.msg}`).join('; ')
          : data.detail
        throw new Error(detail || `HTTP ${resp.status}`)
      }
      return data as T
    } catch (e: any) {
      error.value = e.message || String(e)
      throw e
    } finally {
      loading.value = false
    }
  }

  return { loading, error, keelFetch }
}
