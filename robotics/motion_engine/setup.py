from setuptools import find_packages, setup

package_name = "motion_engine"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools", "numpy"],
    zip_safe=True,
    maintainer="ia-agent",
    maintainer_email="dev@example.com",
    description="Emotion-driven motion synthesis node for NAO V5",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            f"motion_engine = {package_name}.node:main",
        ],
    },
)
