// 主题工具：浅色/深色切换 + localStorage 持久化（默认深色）
export type Theme = 'dark' | 'light'
const KEY = 'dsh-theme'

export function getTheme(): Theme {
  if (typeof localStorage === 'undefined') return 'dark'
  return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
}

export function applyTheme(t: Theme): void {
  document.documentElement.classList.toggle('dark', t === 'dark')
  try {
    localStorage.setItem(KEY, t)
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/** 初始化（main.ts 挂载前调用）：读偏好并应用 */
export function initTheme(): Theme {
  const t = getTheme()
  applyTheme(t)
  return t
}
