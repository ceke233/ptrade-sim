// 手动从 shadcn-vue registry 拉取组件文件（绕过 CLI 在 Windows 上的 nypm 安装 bug）
// 用法: node scripts/fetch-shadcn.mjs badge button card dialog progress table tooltip
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const REGISTRY = 'https://shadcn-vue.com/r/styles/new-york-v4'
const names = process.argv.slice(2)

function transform(content, filePath) {
  // 把 registry 中的别名替换成本地结构
  let c = content
  // registry 文件路径形如 registry/new-york-v4/ui/badge/Badge.vue -> src/components/ui/badge/Badge.vue
  const m = filePath.match(/registry\/[^/]+\/ui\/(.+)$/)
  return { outRel: m ? `src/components/ui/${m[1]}` : null, content: c }
}

for (const name of names) {
  const url = `${REGISTRY}/${name}.json`
  console.log('fetch', url)
  const resp = await fetch(url)
  if (!resp.ok) {
    console.error(`  FAIL ${name}: HTTP ${resp.status}`)
    continue
  }
  const item = await resp.json()
  for (const f of item.files || []) {
    const { outRel, content } = transform(f.content, f.path)
    if (!outRel) {
      console.log('  skip (non-ui file):', f.path)
      continue
    }
    const out = path.join(ROOT, outRel)
    mkdirSync(path.dirname(out), { recursive: true })
    writeFileSync(out, content, 'utf8')
    console.log('  write', outRel, `(${content.length} bytes)`)
  }
}
console.log('done')
