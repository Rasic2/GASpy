from pathlib import Path

from setuptools import setup, find_packages

setup(
    name='GASpy',
    version='0.1.0',
    license='GPL-3.0',
    url='https://github.com/Rasic2/GASpy',
    long_description=Path("./README.md").read_text(encoding="utf-8"),
    long_description_content_type='text/markdown',
    python_requires='>=3.9',
    packages=find_packages(exclude=[]),
    install_requires=[
        'numpy',
        'scipy',
        'ase',
        'pymatgen',
        'pandas',
        'fireworks',
        'tqdm',
        'pymongo',
        'spglib',
        'multiprocess'],
    include_package_data=True,
    package_data={"gaspy": []},
)
