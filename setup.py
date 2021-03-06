#!/usr/bin/env python

from setuptools import setup

setup(
    name='apns2',
    version='0.6.2',
    packages=['apns2'],
    install_requires=[
        'hyper>=0.7',
        'PyJWT>=1.4.0',
        'cryptography>=1.7.2',
    ],
    url='https://github.com/EMMADevelopment/PyAPNs2.git',
    license='MIT',
    author='Sergey Petrov',
    author_email='me@pr0ger.prg',
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Topic :: Software Development :: Libraries',
    ],
    description='A python library for interacting with the Apple Push Notification Service via HTTP/2 protocol'
)
