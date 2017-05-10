import numpy as np

from chainer import cuda

from chainercv.links.faster_rcnn.utils.bbox_regression_target import \
    bbox_regression_target_inv
from chainercv.utils.bbox.non_maximum_suppression import \
    non_maximum_suppression


class ProposalCreator(object):
    """Proposal regions are generated by calling this object.

    The :meth:`__call__` of this object outputs object detection proposals by
    applying estimated bounding-box
    transformations to a set of regular boxes (called "anchors").

    This class is used for Region Proposal Networks introduced in
    Faster RCNN [1].

    .. [1] Shaoqing Ren, Kaiming He, Ross Girshick, Jian Sun. \
    Faster R-CNN: Towards Real-Time Object Detection with \
    Region Proposal Networks. NIPS 2015.


    Args:
        use_gpu_nms (bool): Whether to use GPU powered non maximum
            suppression (NMS) or not when possible. Default value is
            :obj:`True`.
        rpn_min_size (float): Threshold value used when calling NMS.
        train_rpn_pre_nms_top_n (int): Number of top scored bounding boxes
            to keep before passing to NMS in train mode.
        train_rpn_post_nms_top_n (int): Number of top scored bounding boxes
            to keep after passing to NMS in train mode.
        test_rpn_pre_nms_top_n (int): Number of top scored bounding boxes
            to keep before passing to NMS in test mode.
        test_rpn_post_nms_top_n (int): Number of top scored bounding boxes
            to keep after passing to NMS in test mode.
        rpn_min_size (int): A paramter to determine the threshold on
            discarding bounding boxes based on their sizes.

    """

    def __init__(self, use_gpu_nms=True,
                 rpn_nms_thresh=0.7,
                 train_rpn_pre_nms_top_n=12000,
                 train_rpn_post_nms_top_n=2000,
                 test_rpn_pre_nms_top_n=6000,
                 test_rpn_post_nms_top_n=300,
                 rpn_min_size=16):
        self.use_gpu_nms = use_gpu_nms
        self.rpn_nms_thresh = rpn_nms_thresh
        self.train_rpn_pre_nms_top_n = train_rpn_pre_nms_top_n
        self.train_rpn_post_nms_top_n = train_rpn_post_nms_top_n
        self.test_rpn_pre_nms_top_n = test_rpn_pre_nms_top_n
        self.test_rpn_post_nms_top_n = test_rpn_post_nms_top_n
        self.rpn_min_size = rpn_min_size

    def __call__(self, rpn_bbox_pred, rpn_cls_prob,
                 anchor, img_size, scale=1., train=False):
        """Generate deterministic proposal regions.

        The shapes of :obj:`rpn_bbox_pred` and :obj:`rpn_cls_prob` depend on
        the anchors which the Region Proposal Networks is using.

        Here are notations used

        * :math:`A` is number of anchors created for each pixel.
        * :math:`H` and :math:`W` are height and width of the input features.

        Also, the values contained in :obj:`rpn_bbox_pred` is encoded using
        :func:`chainercv.links.faster_rcnn.utils.bbox_regression_target`

        .. seealso::
            :func:`~chainercv.links.faster_rcnn.utils.bbox_regression_target`

        Args:
            rpn_bbox_pred (array): Predicted regression targets for anchors.
                Its shape is :math:`(1, 4 A, H, W)`.
            rpn_cls_prob (array): Predicted foreground probability for anchors.
                Its shape is :math:`(1, 2 A, H, W)`.
            anchor (array): Coordinates of anchors. Its shape is
                :math:`(R, 4)`. The second axis contains x and y coordinates
                of left top vertices and right bottom vertices.
            img_size (tuple of ints): A tuple :obj:`width, height`,
                which contains image size after scaling if any.
            scale (float): The scaling factor used to scale an image after
                reading it from a file.
            train (bool): If this is in train mode or not.
                Default value is :obj:`False`.

        """
        pre_nms_topN = self.train_rpn_pre_nms_top_n \
            if train else self.test_rpn_pre_nms_top_n
        post_nms_topN = self.train_rpn_post_nms_top_n \
            if train else self.test_rpn_post_nms_top_n

        xp = cuda.get_array_module(rpn_cls_prob)
        bbox_deltas = cuda.to_cpu(rpn_bbox_pred.data)
        rpn_cls_prob = cuda.to_cpu(rpn_cls_prob.data)
        anchor = cuda.to_cpu(anchor)
        if not (bbox_deltas.shape[0] == rpn_cls_prob.shape[0] == 1):
            raise ValueError('Only batchsize 1 is supported')

        # the first set of _num_anchors channels are bg probs
        # the second set are the fg probs, which we want
        n_anchor = rpn_cls_prob.shape[1] / 2
        score = rpn_cls_prob[:, n_anchor:, :, :]

        # Transpose and reshape predicted bbox transformations and score
        # to get them into the same order as the anchors:
        bbox_deltas = bbox_deltas.transpose((0, 2, 3, 1)).reshape((-1, 4))
        score = score.transpose((0, 2, 3, 1)).reshape(-1)

        # Convert anchors
        # into proposal via bbox transformations
        proposal = bbox_regression_target_inv(anchor, bbox_deltas)

        # 2. clip predicted boxes to image
        proposal[:, slice(0, 4, 2)] = np.clip(
            proposal[:, slice(0, 4, 2)], 0, img_size[0])
        proposal[:, slice(1, 4, 2)] = np.clip(
            proposal[:, slice(1, 4, 2)], 0, img_size[1])

        # 3. remove predicted boxes with either height or width < threshold
        min_size = self.rpn_min_size * scale
        ws = proposal[:, 2] - proposal[:, 0]
        hs = proposal[:, 3] - proposal[:, 1]
        keep = np.where((ws >= min_size) & (hs >= min_size))[0]
        proposal = proposal[keep, :]
        score = score[keep]

        # 4. sort all (proposal, score) pairs by score from highest to lowest
        # 5. take top pre_nms_topN (e.g. 6000)
        order = score.ravel().argsort()[::-1]
        if pre_nms_topN > 0:
            order = order[:pre_nms_topN]
        proposal = proposal[order, :]
        score = score[order]

        # 6. apply nms (e.g. threshold = 0.7)
        # 7. take after_nms_topN (e.g. 300)
        # 8. return the top proposal (-> RoIs top)
        if self.use_gpu_nms and cuda.available:
            keep = non_maximum_suppression(
                cuda.to_gpu(proposal),
                thresh=self.rpn_nms_thresh,
                score=cuda.to_gpu(score))
            keep = cuda.to_cpu(keep)
        else:
            keep = non_maximum_suppression(
                proposal,
                thresh=self.rpn_nms_thresh,
                score=score)
        if post_nms_topN > 0:
            keep = keep[:post_nms_topN]
        proposal = proposal[keep]

        # Output rois blob
        # Our RPN implementation only supports a single input image, so all
        # batch inds are 0
        batch_ind = np.zeros((proposal.shape[0], 1), dtype=np.float32)
        roi = np.hstack((batch_ind, proposal)).astype(np.float32, copy=False)

        if xp != np:
            roi = cuda.to_gpu(roi)

        return roi
