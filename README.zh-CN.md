# rivetoo-graph

[English](./README.md) | 简体中文

`rivetoo-graph` 是一个显式、可组合的 Python DAG 声明与执行库。

它提供图、步骤、条件依赖、同步与异步执行、运行状态、结果读取和执行视图渲染。
库本身不依赖宿主的日志、终端或请求状态；宿主能力通过独立端口显式接入。

Python 导入入口为：

```python
from rivetoo_graph import Graph, GraphPlan, Step
```

`main` 的声明编译、执行图校验和调度状态机由 Rust 实现；Python 保留声明 API、
任意 callable 的执行与宿主集成。也可以挂载完整 Rust 原生子图，两类 action 共用
执行图与并发预算。实现边界见
[Rust 架构](./docs/zh-CN/rust-architecture.md)。

本地开发需要 Python 3.13 或更高版本、Rust 工具链和 maturin。
激活虚拟环境后，把原生模块安装到该环境：

```bash
maturin develop
```

库没有纯 Python 执行兜底；原生扩展必须可用。

## 简单示例

```python
from rivetoo_graph import GraphPlan, Step

prepare = Step(lambda: print("prepare"), label="prepare")
finish = Step(lambda: print("finish"), label="finish")
graph = (prepare >> finish).graph()
GraphPlan(label="example", graph=graph).run()
```

`>>` 声明先后依赖，`|` 组合无相互依赖的分支；实际并发度由执行选项控制。
普通 action 失败只阻断相关下游，无关分支继续运行，最终汇总报告失败。

完整的模型、组合与执行说明见 [Graph 文档](./docs/zh-CN/index.md)。

## 许可证

本项目采用 [MIT 许可证](./LICENSE)。
