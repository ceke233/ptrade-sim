# web — PTrade 回测看板前端

`ptrade-sim dashboard` 的浏览器端：Vite + Vue 3 + TypeScript + Tailwind v4
+ shadcn-vue（reka-ui）+ ECharts + Pinia。构建产物由 FastAPI 后端（`src/ptrade_sim/server.py`）静态托管。

## 开发

```bash
pnpm install
pnpm dev        # http://localhost:5173，/api 由 vite 代理到 127.0.0.1:8765
```

先把后端跑起来：`ptrade-sim dashboard`。

## 检查与构建

| 命令 | 作用 |
| --- | --- |
| `pnpm run lint` | ESLint 9 扁平配置（`eslint.config.js`） |
| `pnpm run build` | `vue-tsc -b && vite build` —— 类型检查 + 产出 `dist/` |
| `pnpm run preview` | 本地预览构建产物 |

CI（`.github/workflows/ci.yml` 的 `frontend` job）跑的就是
`pnpm install --frozen-lockfile` → `pnpm run lint` → `pnpm run build`。
**改了依赖记得把 `pnpm-lock.yaml` 一起提交**，否则 `--frozen-lockfile` 会失败。

工具链版本要求：Node `^20.19.0 || >=22.12.0`（CI 固定 Node 24 / pnpm 11.8.0，
与 `pnpm-lock.yaml` 的生成版本一致）。

## 构建产物怎么进 wheel

`server.py::_web_dist_candidates()` 按以下顺序找前端目录：

1. `$PTRADE_SIM_WEB_DIST`（显式覆盖）
2. **包目录 `<包>/web/dist`** ← `pip install` 之后的正式路径
3. 仓库根 `web/dist`（开发态）
4. 当前工作目录 `./web/dist`

发布用的 wheel 走第 2 条：CI 的 `build` job 在打包前把 `web/dist` 复制到
`src/ptrade_sim/web/dist`，再由 hatch 的 `packages = ["src/ptrade_sim"]` 一并打进 wheel。
所以**用户 `pip install "ptrade-sim[dashboard]"` 之后不需要 Node 环境就能打开看板**。

本地想复现同一效果：

```bash
cd web && pnpm build
mkdir -p ../src/ptrade_sim/web
rm -rf ../src/ptrade_sim/web/dist
cp -R dist ../src/ptrade_sim/web/dist
cd .. && python -m build --wheel
```

> ⚠️ **开发时的坑**：第 2 条（包目录）**优先于**第 3 条（仓库根 `web/dist`）。
> 所以一旦 `src/ptrade_sim/web/dist` 存在（例如你为了试打包复制过一次），
> 之后 `pnpm build` 的新产物**不会被 serve** —— 页面看起来"改了没生效"。
> 三种解法：删掉包内那份、用 `PTRADE_SIM_WEB_DIST` 显式指向 `web/dist`、
> 或改前端时直接用 `pnpm dev`（走 vite 的 HMR，不经过这个查找）。

⚠️ 打包必须用 `python -m build --wheel`。`python -m build`（默认 sdist+wheel）
会**先构建 sdist、再从 sdist 构建 wheel**，而 sdist 不含构建好的前端
（它的定位是「源码 + 前端源码，用户自行 `pnpm build`」），
那样产出的 wheel 又会丢掉看板。原因详见 `pyproject.toml` 里 wheel 目标的注释。

## 与后端的 API 约定

所有请求都走 `/api` 前缀（见 `src/api/client.ts`），返回类型集中在
`src/api/types.ts`，与 `src/ptrade_sim/server.py` 的路由一一对应 ——
改后端字段时记得同步 `types.ts`。

| 端点 | 说明 |
| --- | --- |
| `GET /api/ping` | 健康检查 / 端口复用探测 |
| `GET /api/runs` | run 列表（状态、进度、实时或最终指标） |
| `GET /api/run/{name}/detail` | 资金曲线 / 回撤 / 月度扩展 / Beta-Alpha / 日志尾 |
| `GET /api/run/{name}/trades?page=&page_size=` | 交易明细（服务端分页） |
| `GET /api/run/{name}/log?lines=&offset=` | 日志尾（可向前翻页） |
| `GET /api/run/{name}/source` | 策略源码 |

数值字段的口径（比例 vs 百分比、null 表示缺失）以后端为准，格式化统一走 `src/lib/format.ts`。

## 目录结构

```
src/
  api/        后端接口客户端与类型定义
  components/ 业务组件（RunCard / TradesTable / SourceDialog / EChart …）
    ui/       shadcn-vue 生成的基础组件（Button/Card/Dialog/Table …）
  lib/        格式化与主题工具（format.ts / theme.ts / utils.ts）
  router/     路由（run 列表 / run 详情）
  stores/     Pinia（runs）
  views/      路由页面
```

`src/components/ui/` 由 shadcn-vue CLI 按 `components.json` 生成：
要改基础组件，优先改配置后用 `pnpm exec shadcn-vue add <name>` 重新生成，
避免后续升级把手工修改覆盖掉。
