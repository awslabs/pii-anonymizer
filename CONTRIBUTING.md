# Contributing Guidelines

Thank you for your interest in contributing to the PII Anonymizer. Whether it is a bug report, new feature, correction, or additional documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary information to effectively respond to your bug report or contribution.

## Quick Start for Contributors

### Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Git

### Setup

```bash
# Clone your fork
git clone https://github.com/<your-username>/pii-anonymizer.git
cd pii_anonymizer

# Install all dependencies (uv recommended)
uv sync --all-extras

# Verify the setup
uv run pytest tests/
```

> Using pip instead of uv? Run `pip install -e ".[all]"`, then `pytest tests/`.

### Development Workflow

1. Create a feature branch from the default branch: `git checkout -b feature/your-feature`
2. Make your changes, keeping them focused on a single concern
3. Run the tests: `uv run pytest tests/`
4. Run linting: `uv run ruff check .` (and `uv run ruff format .` to format)
5. Commit using the conventional format: `feat: add new capability`
6. Open a pull request

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

> **Handling PII in reports.** This is a PII redaction tool. When reporting a bug, do **not** include real personally identifiable information (PII) or protected health information (PHI) in issues, logs, or sample files. Use synthetic or clearly fictional data to reproduce the problem.

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the default branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work. We would hate for your time to be wasted.

### Pull Request Process

To send us a pull request, please:

1. Fork the repository.
2. Create a feature branch from the default branch.
3. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
4. Ensure local tests pass (`uv run pytest tests/`) and linting is clean (`uv run ruff check .`).
5. Commit to your fork using clear, conventional commit messages.
6. Submit your pull request.
7. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

### Pull Request Template

When creating a pull request, please include the following information:

```
*Issue #, if available:*

*Description of changes:*

By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.
```

### Commit Message Guidelines

- Use clear and meaningful commit messages
- Follow the format `type: brief description`
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
- Example: `feat: add validator for a new document type`

Refer to the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/#examples) guide for clear commit guidelines.

## Finding Contributions to Work On

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security Issue Notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
