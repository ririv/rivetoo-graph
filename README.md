# rivetoo-graph

`rivetoo-graph` 是一个显式、可组合的 Python DAG 声明与执行库。

它提供图、步骤、条件依赖、同步与异步执行、运行状态、结果读取和执行视图渲染。
库本身不依赖宿主的日志、终端或请求状态；宿主能力通过独立端口显式接入。

Python 导入入口为：

```python
from rivetoo_graph import Graph, GraphPlan, Step
```

当前 `main` 分支正在把编译执行图迁移到 Rust；迁移前的纯 Python 基线保留在
`python` 分支。Rust 架构与迁移边界见
[Rust 架构](./docs/rust-architecture.md)。

本地开发需要先把原生模块安装到当前 Python 环境：

```bash
maturin develop
```

完整的模型、组合与执行说明见 [Graph 文档](./docs/index.md)。

## 许可证

本项目采用 [MIT 许可证](./LICENSE)。
