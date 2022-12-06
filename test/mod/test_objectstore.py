#
# Tests for the 'osbuild.objectstore' module.
#

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from osbuild import objectstore

from .. import test


def store_path(store: objectstore.ObjectStore, ref: str, path: str) -> bool:
    obj = store.get(ref)
    if not obj:
        return False
    return os.path.exists(os.path.join(obj, path))


@unittest.skipUnless(test.TestBase.can_bind_mount(), "root-only")
class TestObjectStore(unittest.TestCase):

    def setUp(self):
        self.store = tempfile.mkdtemp(prefix="osbuild-test-", dir="/var/tmp")

    def tearDown(self):
        shutil.rmtree(self.store)

    def test_basic(self):
        # always use a temporary store so item counting works
        with objectstore.ObjectStore(self.store) as object_store:
            object_store.maximum_size = 1024*1024*1024

            # No objects or references should be in the store
            assert len(os.listdir(object_store.objects)) == 0

            tree = object_store.new("a")

            # new object should be in write mode
            assert tree.mode == objectstore.Object.Mode.WRITE

            p = Path(tree, "A")
            p.touch()

            tree.finalize()  # put the object into READ mode
            assert tree.mode == objectstore.Object.Mode.READ

            # commit makes a copy, if space
            object_store.commit(tree, "a")
            assert store_path(object_store, "a", "A")

            # second object, based on the first one
            obj2 = object_store.new("b")
            obj2.init(tree)

            p = Path(obj2, "B")
            p.touch()

            obj2.finalize()  # put the object into READ mode
            assert obj2.mode == objectstore.Object.Mode.READ

            # commit always makes a copy, if space
            object_store.commit(tree, "b")

            assert object_store.contains("b")
            assert store_path(object_store, "b", "A")
            assert store_path(object_store, "b", "B")

            assert len(os.listdir(object_store.objects)) == 2

            # object should exist and should be in read mode
            tree = object_store.get("b")
            assert tree is not None
            assert tree.mode == objectstore.Object.Mode.READ

    def test_cleanup(self):
        # always use a temporary store so item counting works
        with objectstore.ObjectStore(self.store) as object_store:
            object_store.maximum_size = 1024*1024*1024

            stage = os.path.join(object_store, "stage")
            tree = object_store.new("a")
            self.assertEqual(len(os.listdir(stage)), 1)
            p = Path(tree, "A")
            p.touch()

        # there should be no temporary Objects dirs anymore
        with objectstore.ObjectStore(self.store) as object_store:
            assert object_store.get("A") is None

    def test_metadata(self):

        # test metadata object directly first
        with tempfile.TemporaryDirectory() as tmp:
            md = objectstore.Object.Metadata(tmp)

            assert os.fspath(md) == tmp

            with self.assertRaises(KeyError):
                with md.read("a"):
                    pass

            # do not write anything to the file, it should not get stored
            with md.write("a"):
                pass

            assert len(list(os.scandir(tmp))) == 0

            # also we should not write anything if an exception was raised
            with self.assertRaises(AssertionError):
                with md.write("a") as f:
                    f.write("{}")
                    raise AssertionError

            with md.write("a") as f:
                f.write("{}")

            assert len(list(os.scandir(tmp))) == 1

            with md.read("a") as f:
                assert f.read() == "{}"

        data = {
            "boolean": True,
            "number": 42,
            "string": "yes, please"
        }

        extra = {
            "extra": "data"
        }

        with tempfile.TemporaryDirectory() as tmp:
            md = objectstore.Object.Metadata(tmp)

            d = md.get("a")
            assert d is None

            md.set("a", None)
            with self.assertRaises(KeyError):
                with md.read("a"):
                    pass

            md.set("a", data)
            assert md.get("a") == data

        with objectstore.ObjectStore(self.store) as store:
            store.maximum_size = 1024*1024*1024
            obj = store.new("a")
            p = Path(obj, "A")
            p.touch()

            obj.meta.set("md", data)
            assert obj.meta.get("md") == data

            store.commit(obj, "x")
            obj.meta.set("extra", extra)
            assert obj.meta.get("extra") == extra

            store.commit(obj, "a")

        with objectstore.ObjectStore(self.store) as store:
            obj = store.get("a")

            assert obj.meta.get("md") == data
            assert obj.meta.get("extra") == extra

            ext = store.get("x")

            assert ext.meta.get("md") == data
            assert ext.meta.get("extra") is None

    def test_host_tree(self):
        with objectstore.ObjectStore(self.store) as store:
            host = store.host_tree

            assert host.tree
            assert os.fspath(host)

            # check we actually cannot write to the path
            p = Path(host.tree, "osbuild-test-file")
            with self.assertRaises(OSError):
                p.touch()
                print("FOO")

        # We cannot access the tree property after cleanup
        with self.assertRaises(AssertionError):
            _ = host.tree

    # pylint: disable=too-many-statements
    def test_store_server(self):

        with contextlib.ExitStack() as stack:

            store = objectstore.ObjectStore(self.store)
            stack.enter_context(store)

            tmpdir = tempfile.TemporaryDirectory()
            tmpdir = stack.enter_context(tmpdir)

            server = objectstore.StoreServer(store)
            stack.enter_context(server)

            client = objectstore.StoreClient(server.socket_address)

            have = client.source("org.osbuild.files")
            want = os.path.join(self.store, "sources")
            assert have.startswith(want)

            tmp = client.mkdtemp(suffix="suffix", prefix="prefix")
            assert tmp.startswith(store.tmp)
            name = os.path.basename(tmp)
            assert name.startswith("prefix")
            assert name.endswith("suffix")

            obj = store.new("42")
            p = Path(obj, "file.txt")
            p.write_text("osbuild")

            p = Path(obj, "directory")
            p.mkdir()
            obj.finalize()

            mountpoint = Path(tmpdir, "mountpoint")
            mountpoint.mkdir()

            assert store.contains("42")
            path = client.read_tree_at("42", mountpoint)
            assert Path(path) == mountpoint
            filepath = Path(mountpoint, "file.txt")
            assert filepath.exists()
            txt = filepath.read_text(encoding="utf8")
            assert txt == "osbuild"

            # check we can mount subtrees via `read_tree_at`

            filemount = Path(tmpdir, "file")
            filemount.touch()

            path = client.read_tree_at("42", filemount, "/file.txt")
            filepath = Path(path)
            assert filepath.is_file()
            txt = filepath.read_text(encoding="utf8")
            assert txt == "osbuild"

            dirmount = Path(tmpdir, "dir")
            dirmount.mkdir()

            path = client.read_tree_at("42", dirmount, "/directory")
            dirpath = Path(path)
            assert dirpath.is_dir()

            # check proper exceptions are raised for non existent
            # mount points and sub-trees

            with self.assertRaises(RuntimeError):
                nonexistent = os.path.join(tmpdir, "nonexistent")
                _ = client.read_tree_at("42", nonexistent)

            with self.assertRaises(RuntimeError):
                _ = client.read_tree_at("42", tmpdir, "/nonexistent")
