// ESLint 扁平配置（ESLint 9）
//
// 目标：在 CI 里挡住「TS 类型错误之外」的前端问题 —— Vue 模板语法/指令误用、
// 未使用变量、误用 any 逃逸类型等。类型本身由 `vue-tsc -b`（见 package.json
// 的 build 脚本）负责，两者互补。
//
// 运行：pnpm run lint（web/ 目录下）
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  // 构建产物与依赖目录不参与检查
  { ignores: ['dist/**', 'node_modules/**'] },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],

  {
    // 浏览器端源码
    files: ['src/**/*.{ts,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser },
    },
  },

  {
    // .vue 单文件组件：<script lang="ts"> 交给 typescript-eslint 解析
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        ecmaVersion: 'latest',
        sourceType: 'module',
        extraFileExtensions: ['.vue'],
      },
    },
    rules: {
      // shadcn-vue 生成的基础组件（Badge / Card / Table …）本身就是单词名，
      // 且它们是随组件库生成的目录，不按业务组件命名规范要求。
      'vue/multi-word-component-names': 'off',
      // 模板里刻意使用动态组件/插槽透传，v-html 由外部数据源控制（本地看板）
      'vue/no-v-html': 'off',
    },
  },

  {
    // 根配置文件与构建脚本跑在 Node 下（vite.config.ts / scripts/*.mjs）
    files: ['*.{ts,js,mjs}', 'scripts/**/*.{js,mjs}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.node },
    },
  },

  {
    files: ['**/*.{ts,vue}'],
    rules: {
      // 允许以 `_` 前缀显式标记「有意不用」的解构占位/参数
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
        },
      ],
    },
  },

  {
    // shadcn-vue 生成的基础组件：registry 里的类型签名存在无法避免的 any，
    // 且这部分是「CLI 生成 + 少量本地调整」，放松为警告而不是直接挡 CI。
    // 业务代码（api / lib / stores / views / 其余 components）仍按
    // typescript-eslint recommended 的 error 级别要求（当前为 0 处 any）。
    files: ['src/components/ui/**/*.{ts,vue}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
)
