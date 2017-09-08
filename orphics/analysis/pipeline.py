from __future__ import print_function
import numpy as np
import sys
from enlib import enmap,powspec
from orphics.tools.stats import timeit
import commands
import logging

class MPIStats(object):
    """
    A helper container for
    1) 1d measurements whose statistics needs to be calculated
    2) 2d cumulative stacks

    where different MPI cores may be calculating different number
    of 1d measurements or 2d stacks.
    """
    
    def __init__(self,comm,num_each,tag_start=333):
        """
        comm - MPI.COMM_WORLD object
        num_each - 1d array or list where the ith element indicates number of tasks assigned to ith core
        tag_start - MPI comm tags start at this integer
        """
        
        self.comm = comm
        self.num_each = num_each
        self.rank = comm.Get_rank()
        self.numcores = comm.Get_size()    
        self.vectors = {}
        self.little_stack = {}
        self.little_stack_count = {}
        self.tag_start = tag_start

    def add_to_stats(self,label,vector):
        """
        Append the 1d vector to a statistic named "label".
        Create a new one if it doesn't already exist.
        """
        
        if not(label in self.vectors.keys()): self.vectors[label] = []
        self.vectors[label].append(vector)


    def add_to_stack(self,label,arr):
        """
        This is just an accumulator, it can't track statisitics.
        Add arr to a cumulative stack named "label". Could be 2d arrays.
        Create a new one if it doesn't already exist.
        """
        if not(label in self.little_stack.keys()):
            self.little_stack[label] = 0.
            self.little_stack_count[label] = 0
        self.little_stack[label] += arr
        self.little_stack_count[label] += 1


    def get_stacks(self,verbose=True):
        """
        Collect from all MPI cores and calculate stacks.
        """
        if self.rank!=0:

            for k,label in enumerate(self.little_stack.keys()):
                self.comm.send(self.little_stack_count[label], dest=0, tag=self.tag_start*300+k)
            
            for k,label in enumerate(self.little_stack.keys()):
                send_dat = np.array(self.little_stack[label]).astype(np.float64)
                self.comm.Send(send_dat, dest=0, tag=self.tag_start*10+k)

        else:
            self.stacks = {}
            self.stack_count = {}

            for k,label in enumerate(self.little_stack.keys()):
                self.stack_count[label] = self.little_stack_count[label]
                for core in range(1,self.numcores):
                    data = self.comm.recv(source=core, tag=self.tag_start*300+k)
                    self.stack_count[label] += data

            
            for k,label in enumerate(self.little_stack.keys()):
                self.stacks[label] = self.little_stack[label]
            for core in range(1,self.numcores):
                if verbose: print ("Waiting for core ", core , " / ", self.numcores)
                for k,label in enumerate(self.little_stack.keys()):
                    expected_shape = self.little_stack[label].shape
                    data_vessel = np.empty(expected_shape, dtype=np.float64)
                    self.comm.Recv(data_vessel, source=core, tag=self.tag_start*10+k)
                    self.stacks[label] += data_vessel

                    
            for k,label in enumerate(self.little_stack.keys()):                
                self.stacks[label] /= self.stack_count[label]
                
    def get_stats(self,verbose=True):
        """
        Collect from all MPI cores and calculate statistics for
        1d measurements.
        """
        import orphics.tools.stats as stats
        
        if self.rank!=0:
            for k,label in enumerate(self.vectors.keys()):
                self.comm.send(np.array(self.vectors[label]).shape[0], dest=0, tag=self.tag_start*200+k)

            for k,label in enumerate(self.vectors.keys()):
                send_dat = np.array(self.vectors[label]).astype(np.float64)
                self.comm.Send(send_dat, dest=0, tag=self.tag_start+k)

        else:
            self.stats = {}
            self.numobj = {}
            for k,label in enumerate(self.vectors.keys()):
                self.numobj[label] = []
                self.numobj[label].append(np.array(self.vectors[label]).shape[0])
                for core in range(1,self.numcores):
                    data = self.comm.recv(source=core, tag=self.tag_start*200+k)
                    self.numobj[label].append(data)

            
            for k,label in enumerate(self.vectors.keys()):
                self.vectors[label] = np.array(self.vectors[label])
            for core in range(1,self.numcores):
                if verbose: print ("Waiting for core ", core , " / ", self.numcores)
                for k,label in enumerate(self.vectors.keys()):
                    expected_shape = (self.numobj[label][core],self.vectors[label].shape[1])
                    data_vessel = np.empty(expected_shape, dtype=np.float64)
                    self.comm.Recv(data_vessel, source=core, tag=self.tag_start+k)
                    self.vectors[label] = np.append(self.vectors[label],data_vessel,axis=0)

            for k,label in enumerate(self.vectors.keys()):
                self.stats[label] = stats.getStats(self.vectors[label])
            #self.vectors = {}
                
def mpi_distribute(num_tasks,avail_cores):
    assert avail_cores<=num_tasks
    min_each, rem = divmod(num_tasks,avail_cores)
    num_each = np.array([min_each]*avail_cores) # first distribute equally
    if rem>0: num_each[-rem:] += 1  # add the remainder to the last set of cores (so that rank 0 never gets extra jobs)

    task_range = range(num_tasks) # the full range of tasks
    cumul = np.cumsum(num_each).tolist() # the end indices for each task
    task_dist = [task_range[x:y] for x,y in zip([0]+cumul[:-1],cumul)] # a list containing the tasks for each core
    return num_each,task_dist


class Pipeline(object):
    """ This class allows flexible distribution of N tasks across m MPI cores.
    'Flexible' here means that options to keep some cores idle in order to
    conserve memory are provided. This class and its subclasses will/should 
    ensure that the idle cores are not involved in MPI communications.

    In its simplest form, there are N tasks and m MPI cores. quotient(N/m) tasks 
    will be run on each core in series plus one extra job for the remainder(N/m)
    cores.

    But say you have 10 nodes with 8 cores each, and 160 tasks. You could
    say N=160 and pass the MPI Comm object which tells the class that m=80.
    But you don't want to use all cores because the node memory is limited.
    You only want to use 4 cores per node. Then you specify:

    stride = 2

    This will "stride" the MPI cores by k = 2 involving only ever other MPI core 
    in communications. This means that the number of available cores j = m/k = 40. 
    So the number of serial tasks is now 4 instead of 2.


    The base class Pipeline just implements the job distribution and striding. It is
    up to the derived classes to overload the "task" function and actually
    do something.
    
    """
    

    def __init__(self,MPI_Comm_World,num_tasks,stride=None):

        wcomm = MPI_Comm_World
        wrank = wcomm.Get_rank()
        wsize = wcomm.Get_size()
        self.wsize = wsize

        
        if stride is not None:
            # we want to do some striding
            assert wsize%stride==0
            
        else:
            # no striding
            stride = 1

        avail_cores = wsize/stride

        participants = range(0,wsize,stride) # the cores that participate in MPI comm
        
        task_ids = range(num_tasks)
        # min_each, rem = divmod(num_tasks,avail_cores)
        # num_each = np.array([min_each]*avail_cores) # first distribute equally
        # if rem>0: num_each[-rem:] += 1  # add the remainder to the last set of cores (so that rank 0 never gets extra jobs)
        num_each,task_dist = mpi_distribute(num_tasks,avail_cores)

        assert sum(num_each)==num_tasks
        self.num_each = num_each

        if wrank in participants:
            self.mcomm = MPI_Comm_World.Split(color=55,key=wrank)
            self.mrank = self.mcomm.Get_rank()
            self.msize = self.mcomm.Get_size()
            self.num_mine = num_each[self.mrank]
            
        else:
            idlecomm = MPI_Comm_World.Split(color=75,key=-wrank)
            sys.exit(1)



    def distribute(self,array,tag):
        """ Send array from rank=0 to all participating MPI cores.
        """
        if self.mrank==0:
            for i in range(self.msize):
                self.mcomm.Send(array,dest=i,tag=tag)
        else:
            self.mcomm.Recv(array,source=0,tag=tag)
        return array
        
    def info(self):
        if self.mrank==0:
            print ("Jobs in each core :", self.num_each)
        print ("==== My rank is ", self.mrank, ". Hostname: ", commands.getoutput("hostname") ,". I have ", self.num_mine, " tasks. ===")

    def task(self):
        pass
    def _initialize(self):
        pass
    def _finish(self):
        pass
    
    def loop(self):
        self._initialize()
        for i in range(self.num_mine):
            self.task(i)
        self._finish()
            

class CMB_Pipeline(Pipeline):
    def __init__(self,MPI_Comm_World,num_tasks,cosmology,patch_shape,patch_wcs,stride=None):
        super(CMB_Pipeline, self).__init__(MPI_Comm_World,num_tasks,stride)
        self.shape = patch_shape
        self.wcs = patch_wcs
        self.ps = powspec.read_spectrum("../alhazen/data/cl_lensinput.dat") # !!!!

        self.info()
        
    @timeit
    def get_unlensed(self,pol=False):
        self.unlensed = enmap.rand_map(self.shape, self.wcs, self.ps)

    def task(self,i):
        u = self.get_unlensed()
    
        
class CMB_Lensing_Pipeline(CMB_Pipeline):
    def __init__(self,MPI_Comm_World,num_tasks,cosmology,patch_shape,patch_wcs,
                 input_kappa=None,input_phi=None,input_disp_map=None,stride=None):
        super(CMB_Lensing_Pipeline, self).__init__(MPI_Comm_World,num_tasks,cosmology,patch_shape,patch_wcs,stride)


        assert count_not_nones([input_kappa,input_phi,input_disp])<=1
        
        if input_kappa is not None:
            fkappa = fft(input_kappa)
            self.disp_pix = ifft(ells*fkappa/ell**2.)
        elif input_phi is not None:
            fphi = fft(input_phi)
            #self.disp_pix = 
        elif input_disp_map is not None:
            self.disp_pix = input_disp_map

    # def lens_map(self,unlensed=self.unlensed,disp_pix=self.disp_pix):
    #     assert disp_pix is not None,"No input displacement specified."
        
    


def is_only_one_not_none(a):
    """ Useful for function arguments, returns True if the list 'a'
    contains only one non-None object and False if otherwise.

    Examples:

    >>> is_only_one_not_none([None,None,None])
    >>> False
    >>> is_only_one_not_none([None,1,1,4])
    >>> False
    >>> is_only_one_not_none([1,None,None,None])
    >>> True
    >>> is_only_one_not_none([None,None,6,None])
    >>> True
    """
    return True if count_not_nones(a)==1 else False

def count_not_nones(a):
    return sum([int(x is not None) for x in a])


# class CMB_Array_Patch(object):
#     """ Make one of these for each array+patch combination.
#     Maximum memory used should be = 

#     """
    

#     def __init__(self,enmap_patch_template,beam2d=None,beam_arcmin=None,beam_tuple=None,
#                  noise_uk_arcmin=None,lknee=None,alpha=None,noise2d=None,hit_map=None):

#         assert is_only_one_not_none([beam2d,beam_arcmin,beam_tuple]), "Multiple or no beam options specified"
#         if noise2d is not None:
#             # 2d noise specified
#             assert noise_uk_arcmin is None and lknee is None and alpha is None
#         else:
#             pass

# def get_unlensed_cmb(patch,cosmology,pol=False):
#     cmaps = grf.make_map(patch,cosmology,pol=pol)
#     if self.verify_unlensed_power: self.upowers.append(get_cmb_powers(cmaps,self.taper))
#     return cmaps





# if test=="liteMap_file":
#     template_liteMap_path = ""
#     lmap = liteMap.liteMapFromFits(template_liteMap_path)
#     template_enmap = enmap_from_liteMap(lmap)
    
# elif test=="wide_patch":
#     shape, wcs = enmap.geometry(pos=[[-hwidth*arcmin,-hwidth*arcmin],[hwidth*arcmin,hwidth*arcmin]], res=px*arcmin, proj="car")

    
# elif test=="cluster_patch":


# test_array = CMB_Array_Patch(template_enmap,beam_arcmin=1.4,noise_uk_arcmin=20.,lknee=3000,alpha=-4.6)
    
