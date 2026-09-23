TSINGHUA_PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
TSINGHUA_TRUSTED_HOST = "pypi.tuna.tsinghua.edu.cn"
FALLBACK_PROXY = "http://100.64.1.68:1080"


def retry_with_proxy(cmd: str, delay: int = 5) -> str:
    return (
        f"{{ {cmd}; }} || "
        f"{{ echo 'fallback proxy retry 1/2'; sleep {delay}; "
        f"http_proxy={FALLBACK_PROXY} https_proxy={FALLBACK_PROXY} {cmd}; }} || "
        f"{{ echo 'fallback proxy retry 2/2'; sleep {delay}; "
        f"http_proxy={FALLBACK_PROXY} https_proxy={FALLBACK_PROXY} {cmd}; }}"
    )
