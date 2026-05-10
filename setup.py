from setuptools import setup


setup(
    name="link2md",
    version="0.1.0",
    description="Convert Xiaohongshu, Bilibili, and Douyin links into Markdown notes.",
    py_modules=["link2md"],
    python_requires=">=3.9",
    entry_points={"console_scripts": ["link2md=link2md:main"]},
)
