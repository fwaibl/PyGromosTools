"""
File:            Class for trc files in pandas
Description:
    The pandas trajectory TRC class offers a easy method to process GROMOS's .trc files in python
    The trc files are parsed into an easy to use pandas dataframe

Author: Marc Thierry Lehner

TODO: add stride option to all member functions
TODO: add support for periodic boundary condition
TODO: add support for rdkit mol selector
TODO: add support for rdkit conformers

"""

# imports
import tempfile
import mdtraj
import pandas as pd
import numpy as np
import nglview as nj
from copy import deepcopy
from typing import Dict, List, Tuple
from pygromos.utils import bash
from pygromos.files.blocks._general_blocks import TITLE
from pygromos.files.coord.cnf import Cnf
from pygromos.files.blocks.coord_blocks import POSITION
from pygromos.visualization.coordinates_visualization import visualize_system


class Trc(mdtraj.Trajectory):
    # Attributes
    TITLE: TITLE

    _future_file: bool
    path: str  # Todo: we need to set this variable.

    def __init__(
            self,
            xyz=None,
            topology=None,
            time=None,
            unitcell_lengths=None,
            unitcell_angles=None,
            traj_path=None,
            in_cnf: [str, Cnf] = None,
    ):

        self._future_file = False

        if traj_path is not None and (traj_path.endswith(".h5") or traj_path.endswith(".hf5")):
            trj = self.load(traj_path)
            self.__dict__.update(vars(trj))

        elif traj_path is not None and (traj_path.endswith(".trc") or traj_path.endswith(".trc.gz")):

            # Parse TRC
            compress = False
            if traj_path.endswith(".gz"):
                traj_path = bash.compress_gzip(in_path=traj_path, extract=True)
                compress = True

            unitcell_angles = None
            unitcell_lengths = None

            if isinstance(traj_path, str):
                xyz, step, time, unitcell_lengths, unitcell_angles = self.parse_trc_efficiently(traj_path)

            if compress:
                traj_path = bash.compress_gzip(in_path=traj_path)

            # Topology from Cnf
            if isinstance(in_cnf, str):
                in_cnf = Cnf(in_cnf)
            elif isinstance(in_cnf, Cnf) and hasattr(in_cnf, "POSITION"):
                pass
            else:
                in_cnf = self.get_dummy_cnf(xyz)

            # get cnf boxDims
            if hasattr(in_cnf, "GENBOX") and (unitcell_lengths is None and unitcell_angles is None):
                unitcell_angles = np.array(list(in_cnf.GENBOX.angles) * len(xyz)).reshape(
                    len(xyz), len(in_cnf.GENBOX.length)
                )
                unitcell_lengths = np.array(list(in_cnf.GENBOX.length) * len(xyz)).reshape(
                    len(xyz), len(in_cnf.GENBOX.length)
                )

            # Topo tmp file
            tmpFile = tempfile.NamedTemporaryFile(suffix="_tmp.pdb")
            in_cnf.write_pdb(tmpFile.name)
            single = mdtraj.load_pdb(tmpFile.name)
            tmpFile.close()

            super().__init__(
                xyz=xyz,
                topology=single.topology,
                time=time,
                unitcell_lengths=unitcell_lengths,
                unitcell_angles=unitcell_angles,
            )
            self._step = step
        elif not (xyz is None and topology is None):
            super().__init__(
                xyz=xyz,
                topology=topology,
                time=time,
                unitcell_lengths=unitcell_lengths,
                unitcell_angles=unitcell_angles,
            )

        else:
            self._unitcell_lengths = []
            self._unitcell_angles = []
            self._xyz = np.array([], ndmin=2)
            self._topology = None
            self._future_file = True

        self.path = traj_path

    def __copy__(self):
        attribs = {
            "xyz": deepcopy(self._xyz),
            "topology": deepcopy(self._topology),
            "unitcell_lengths": deepcopy(self._unitcell_lengths),
            "unitcell_angles": deepcopy(self._unitcell_angles),
            "traj_path": deepcopy(self.path),
            "_future_file": self._future_file,
        }
        for additional_key in ["unitcell_angles", "unitcell_angles"]:
            if hasattr(self, additional_key) and getattr(self, additional_key) is not None:
                attribs.update({additional_key: deepcopy(getattr(self, additional_key))})
                
        cCls = self.__class__(**attribs)
        cCls._step = deepcopy(self._step)
        cCls.TITLE = deepcopy(self.TITLE)
        cCls._time = deepcopy(self._time)
        
        return 

    def __getitem__(self, key):
        print(vars(self).keys())
        t = self.slice(key)
        if(hasattr(t, "_step")): t._step = deepcopy(self._step[key])
        if(hasattr(t, "_time")): t._time = deepcopy(self._time[key])
        if(hasattr(t, "TITLE")): t.TITLE = deepcopy(self.TITLE)
        
        return t

    def get_dummy_cnf(self, xyz) -> Cnf:
        from pygromos.files.blocks import coords

        new_Cnf = Cnf(None)
        new_Cnf.add_block(blocktitle="TITLE", content="THis is a dummy top depending on the first Frame.")
        new_Cnf.add_block(
            blocktitle="POSITION",
            content=[
                coords.atomP(resID=1, resName="DUM", atomType="C", atomID=i, xp=coord[0], yp=coord[1], zp=coord[2])
                for i, coord in enumerate(xyz[0])
            ],
        )
        return new_Cnf

    def parse_trc_efficiently(self, traj_path: str) -> (np.array, np.array, np.array):
        self._block_map = self._generate_blockMap(in_trc_path=traj_path)
        # build block mapping
        rep_time = 1
        start = self._block_map["TIMESTEP"]
        title = self._block_map["TITLE"]
        end = start + self._block_map["POSITIONRED"] - 1
        timestep_block_length = sum(
            [self._block_map[key] for key in self._block_map if (key not in ["TITLE", "commentLines"])]
        )
        chunk = self._block_map["POSITIONRED"] - self._block_map["commentLines"] - 2 + 1

        # block mapping logic
        base_rows = (
            lambda x: not (
                    (((x - title) % timestep_block_length > start) and ((x - title) % timestep_block_length < end))
                    or (x - title) % timestep_block_length == rep_time
            )
            if x > title
            else True
        )

        unitcell_length = None
        unitcell_angles = None
        genbox_present = False
        if "GENBOX" in self._block_map:
            genbox_present = True
            chunk += 2
            # timestep_block_length += 7
            skip_rows1 = lambda x: (
                    base_rows(x)
                    and not ((x - title) % timestep_block_length == end + 3)
                    and not ((x - title) % timestep_block_length == end + 4)
            )

            skip_rows = lambda x: (x < title) or skip_rows1(x)

            unitcell_length = []
            unitcell_angles = []
        else:
            skip_rows = lambda x: (x < title) or base_rows(x)

        # parsing
        data = []
        time = []
        step = []
        for b in pd.read_table(
                traj_path, delim_whitespace=True, skiprows=skip_rows, names=["x", "y", "z"], chunksize=chunk,
                comment="#"
        ):
            if genbox_present:
                data.append(b.values[1:-2, :])
                time.append(b.values[0, 1])
                step.append(b.values[0, 0])
                unitcell_length.append(b.values[-2, :])
                unitcell_angles.append(b.values[-1, :])
            else:
                data.append(b.values[1:, :])
                time.append(b.values[0, 1])
                step.append(b.values[0, 0])

        # make np.Arrays
        xyz = np.array(data)
        time = np.array(time)
        step = np.array(step)
        if genbox_present:
            unitcell_length = np.array(unitcell_length)
            unitcell_angles = np.array(unitcell_angles)

        return xyz, step, time, unitcell_length, unitcell_angles

    @property
    def step(self) -> np.array:
        return self._step

    # Analysis of traj
    def rmsd(self, reference_frame: int = 0, reference: mdtraj.Trajectory = None) -> pd.DataFrame:
        if reference is None:
            reference = self
        time_scale = pd.Series(data=self.time, name="time")
        return pd.DataFrame({"rmsd": mdtraj.rmsd(self, reference, reference_frame)}, index=time_scale)

    def distances(
            self,
            atom_pairs: List[Tuple[int, int]],
            periodic: bool = True,
            opt: bool = True,
    ) -> pd.DataFrame:
        arr = mdtraj.compute_distances(self, atom_pairs=atom_pairs, periodic=periodic, opt=opt)
        time_scale = pd.Series(data=self.time, name="time")
        return pd.DataFrame(
            {str(key[0]) + "-" + str(key[1]): val for key, val in zip(atom_pairs, arr.T)}, index=time_scale
        )

    def _generate_blockMap(self, in_trc_path: str) -> Dict[str, int]:
        block_map = {}
        with open(in_trc_path, "r") as file_handle:
            inBlock = False
            inTitleBlock = False
            blockKey = None
            nLines = 0
            nCommentLines = 0
            titleStr = []

            max_it = 1000000
            i = 0
            while max_it > i:
                line = file_handle.readline().strip()

                if line.startswith("#"):
                    nCommentLines += 1

                if "END" == line.strip():
                    block_map.update({blockKey: nLines})
                    if blockKey == TITLE.__name__:
                        inTitleBlock = False
                        self.TITLE = TITLE(content="\n".join(titleStr))
                    inBlock = False

                elif not inBlock:
                    blockKey = line.strip()
                    if blockKey in block_map:
                        break
                    elif blockKey == TITLE.__name__:
                        inTitleBlock = True
                    inBlock = True
                    nLines = 1

                elif inTitleBlock:
                    titleStr.append(line)

                i += 1  # this is a potential danger
                nLines += 1

        if not hasattr(self, TITLE.__name__):
            self.TITLE = TITLE(content="Empty TITLE")

        block_map.update({"commentLines": nCommentLines})

        return block_map

    # Visualizaton
    @property
    def view(self, re_create: bool = False) -> nj.NGLWidget:
        if not hasattr(self, "_view") or not isinstance(self._view, nj.NGLWidget) or re_create:
            self._view = nj.show_mdtraj(self)
            self.view.clear_representations()
            self.view.add_representation("licorice", selection="all")
        return self._view

    def recreate_view(self) -> nj.NGLWidget:
        self._view = visualize_system(traj=self)
        return self._view

    # io
    def write_trc(self, out_path: str) -> str:

        first_entry = self.generate_first_entry()
        array_list = [first_entry]

        for i in range(len(self.time) - 1):
            array_list.append(self.generate_entry_for_frame(i))

        array = np.concatenate(array_list, axis=0)
        array = np.array(array, dtype=str)
        array[array == 'None'] = ''

        np.savetxt(out_path, array, fmt='%s', delimiter='\t')

    def generate_entry_for_frame(self, frame_id: int):

        length = 3  # TIMESTEP
        length += 2  # Positionred
        length += self.xyz[0].shape[0]

        # Add comments
        length += self.xyz[0].shape[0] // 10

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            length += 7

        array = np.empty((length, 4), dtype=object)

        # Add TIMESTEP
        array[0, 0] = "TIMESTEP"
        array[1, 1] = self.step[frame_id + 1]
        array[1, 2] = self.time[frame_id + 1]
        array[2, 0] = "END"

        # Add POSITIONRED
        array[3, 0] = "POSITIONRED"
        start = 4
        for block in range(self.xyz[frame_id].shape[0] // 10):
            array[start + block * 10: start + (block + 1) * 10, 1:] = self.xyz[frame_id][block * 10: (block + 1) * 10,
                                                                      :]
            array[start + (block + 1) * 10, 0] = "#"
            array[start + (block + 1) * 10, 1] = (block + 1) * 10
            start += 1

        last = self.xyz[frame_id].shape[0] % 10

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            array_last = last + 7
        else:
            array_last = last

        array[-(array_last + 1):-(array_last - last) - 1, 1:] = self.xyz[frame_id][-last:, :]
        array[-(array_last - last) - 1, 0] = "END"

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            array[-(array_last - last), 0] = "GENBOX"
            array[-(array_last - last) + 1, 1] = 1
            array[-(array_last - last) + 2, 1:] = self.unitcell_lengths[frame_id]
            array[-(array_last - last) + 3, 1:] = self.unitcell_angles[frame_id]
            array[-(array_last - last) + 4, 1:] = 0
            array[-(array_last - last) + 5, 1:] = 0
            array[-(array_last - last) + 6, 0] = "END"

        return array

    def generate_first_entry(self):
        length = 3  # TITLE will only be one line
        length += 3  # TIMESTEP
        length += 2
        length += self.xyz[0].shape[0]

        # Add comments
        length += self.xyz[0].shape[0] // 10

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            length += 7

        array = np.empty((length, 4), dtype=object)

        # Add Title
        array[0, 0] = "TITLE"

        titlestring = ''
        for t in self.TITLE.content:
            titlestring += t

        array[1, 0] = titlestring
        array[2, 0] = "END"

        # Add TIMESTEP
        array[3, 0] = "TIMESTEP"
        array[4, 1] = self.timestep * 0
        array[4, 2] = self.time[0]
        array[5, 0] = "END"

        # Add POSITIONRED
        array[6, 0] = "POSITIONRED"
        start = 7
        for block in range(self.xyz[0].shape[0] // 10):
            array[start + block * 10: start + (block + 1) * 10, 1:] = self.xyz[0][block * 10: (block + 1) * 10, :]
            array[start + (block + 1) * 10, 0] = "#"
            array[start + (block + 1) * 10, 1] = (block + 1) * 10
            start += 1

        last = self.xyz[0].shape[0] % 10

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            array_last = last + 7
        else:
            array_last = last

        array[-(array_last + 1):-(array_last - last) - 1, 1:] = self.xyz[0][-last:, :]
        array[-(array_last - last) - 1, 0] = "END"

        if not (self.unitcell_lengths is None and len(self.unitcell_lengths) > 0):
            array[-(array_last - last), 0] = "GENBOX"
            array[-(array_last - last) + 1, 1] = 1
            array[-(array_last - last) + 2, 1:] = self.unitcell_lengths[0]
            array[-(array_last - last) + 3, 1:] = self.unitcell_angles[0]
            array[-(array_last - last) + 4, 1:] = 0
            array[-(array_last - last) + 5, 1:] = 0
            array[-(array_last - last) + 6, 0] = "END"

        return array

    def to_conf(self, frame_id: int = None, base_cnf: Cnf = None):
        from pygromos.files.blocks import coords

        if frame_id is None:
            frame_id = 0

        content_str = "THIS IS THE FRAME AT TIMESTEP: " + str(
            self.time[frame_id]) + " OF THE TRAJECTORY WITH TITLE:\n" + self.TITLE.block_to_string()

        if base_cnf is None:
            new_Cnf = self.get_dummy_cnf(self.xyz)
        else:
            if type(base_cnf) is Cnf:
                new_Cnf = base_cnf
            else:
                new_Cnf = Cnf(base_cnf)
        new_Cnf.TITLE = TITLE(content_str)

        new_Cnf.POSITION = POSITION([coords.atomP(resID=new_Cnf.POSITION.content[i].resID,
                                                  resName=new_Cnf.POSITION.content[i].resName,
                                                  atomType=new_Cnf.POSITION.content[i].atomType, atomID=i, xp=coord[0],
                                                  yp=coord[1], zp=coord[2])
                                     for i, coord in enumerate(self.xyz[frame_id])])
        
        if(hasattr(new_Cnf, "GENBOX")):
            new_Cnf.GENBOX.length = list(self.unitcell_lengths[frame_id])
            new_Cnf.GENBOX.angles = list(self.unitcell_angles[frame_id])
            new_Cnf.GENBOX.euler = [0,0,0]
            new_Cnf.GENBOX.origin = [0,0,0]
        else:
            from pygromos.files.blocks.coord_blocks import GENBOX
            box_block = GENBOX(pbc=1, length=list(self.unitcell_lengths[frame_id]) ,angles=list(self.unitcell_angles[frame_id]))
            new_Cnf.add_block(block=box_block)


        return new_Cnf

    def save(self, out_path: str) -> str:

        compress = False
        if out_path.endswith(".trc.gz"):
            compress = True
            out_path.replace(".gz", "")

        # write out
        if out_path.endswith(".trc"):
            out_path = self.write_trc(out_path)
            # compress if desired
            if compress:
                out_path = bash.compress_gzip(in_path=out_path)
            return out_path
        else:
            super().save(out_path)
            return out_path

    def write(self, out_path: str) -> str:
        return self.save(out_path)
